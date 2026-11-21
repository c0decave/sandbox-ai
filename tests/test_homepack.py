"""homepack.py: a stopped kept start's HOME packed into one archive and unpacked again - whole (files, folders,
links, hard links, modes, times), verified by its digest before anything is removed, nothing lost on a failure or an
interruption, never following a link. Only temporary trees; zstd is faked (gzip) where it is not installed."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_homepack_test", ROOT / "sandbox/homepack.py")
hp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hp)

FAKE_ZSTD = f"""#!{sys.executable}
import gzip, sys
args = sys.argv[1:]
if "-d" in args:
    sys.stdout.buffer.write(gzip.decompress(open(args[-1], "rb").read()))
else:
    sys.stdout.buffer.write(gzip.compress(sys.stdin.buffer.read(), 1))
"""


@pytest.fixture(params=["xz", "zstd"])
def compressor(request, tmp_path, monkeypatch):
    if request.param == "xz":
        monkeypatch.setattr(hp, "zstd", lambda: None)
    else:
        fake = tmp_path / "bin/zstd"
        fake.parent.mkdir()
        fake.write_text(FAKE_ZSTD)
        fake.chmod(0o755)
        monkeypatch.setattr(hp, "zstd", lambda: str(fake))
    return request.param


def make_home(directory):
    home = directory / "home"
    (home / ".cache/pip").mkdir(parents=True)
    (home / ".cache/pip/wheel").write_bytes(os.urandom(50_000))
    (home / "work").mkdir()
    (home / "work/notes.md").write_text("hello\n")
    (home / "work/run.sh").write_text("#!/bin/sh\n")
    (home / "work/run.sh").chmod(0o755)
    os.utime(home / "work/notes.md", (1_700_000_000, 1_700_000_000))
    os.link(home / "work/notes.md", home / "work/notes-again.md")
    (home / "work/to-etc").symlink_to("/etc/hostname")
    (home / "work/relative").symlink_to("notes.md")
    (home / "go/pkg/mod/x").mkdir(parents=True)
    (home / "go/pkg/mod/x/a.go").write_text("package x\n")
    (home / "go/pkg/mod/x").chmod(0o555)  # read-only, as Go's module cache
    (home / "empty").mkdir()
    os.mkfifo(home / "work/fifo")
    (home / "naïve name ü.txt").write_text("ü")
    return home


def tree(home):
    """{relative path: (type, mode, size, content or link)} of HOME."""
    found = {}
    for folder, dirs, files in os.walk(home):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.lstat()
            kind = "link" if path.is_symlink() else "dir" if path.is_dir() else "fifo" if \
                stat.S_ISFIFO(info.st_mode) else "file"
            what = os.readlink(path) if kind == "link" else path.read_bytes() if kind == "file" else None
            found[str(path.relative_to(home))] = (kind, stat.S_IMODE(info.st_mode) if kind != "link" else 0,
                                                  info.st_size if kind == "file" else 0, what)
    return found


def test_pack_and_unpack_keep_the_home_whole(tmp_path, compressor):
    directory = tmp_path / "start"
    directory.mkdir()
    home = make_home(directory)
    before = tree(home)
    record = hp.pack(directory, size=123)
    assert not home.exists() and hp.packed(directory) == record and hp.state(directory) == "packed"
    assert record["archive"] == ("home.tar.zst" if compressor == "zstd" else "home.tar.xz")
    assert record["bytes"] == 123 and record["entries"] == len(before) and len(record["digest"]) == 64
    assert (directory / record["archive"]).stat().st_size == record["archive_bytes"]
    assert [p.name for p in directory.iterdir() if p.name.startswith(".")] == []
    assert hp.unpack(directory)["digest"] == record["digest"]
    assert tree(home) == before and hp.state(directory) == "unpacked"
    assert os.stat(home / "work/notes.md").st_ino == os.stat(home / "work/notes-again.md").st_ino
    assert (home / "work/notes.md").stat().st_mtime == 1_700_000_000
    assert not (directory / hp.META).exists() and not hp.archives(directory)
    assert hp.unpack(directory) is None  # not packed: nothing to do
    hp.remove_tree(home)  # (the read-only folder too)
    assert not home.exists()


def test_pack_never_follows_links_and_skips_sockets(tmp_path, compressor):
    directory = tmp_path / "start"
    home = directory / "home"
    home.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("not in the archive")
    (home / "link").symlink_to(outside)
    listening = socket.socket(socket.AF_UNIX)
    listening.bind(str(home / "agent.sock"))
    try:
        record = hp.pack(directory)
    finally:
        listening.close()
    assert record["skipped"] == ["agent.sock"] and record["entries"] == 1
    assert outside.joinpath("secret").exists()  # the link's target untouched
    hp.unpack(directory)
    assert (home / "link").is_symlink() and os.readlink(home / "link") == str(outside)


def test_a_pack_that_fails_keeps_the_home(tmp_path, compressor, monkeypatch):
    directory = tmp_path / "start"
    directory.mkdir()
    home = make_home(directory)
    before = tree(home)
    unreadable = home / "work/locked.txt"
    unreadable.write_text("x")
    unreadable.chmod(0)
    if os.geteuid() != 0:
        with pytest.raises(hp.PackError, match="the HOME stays as it was"):
            hp.pack(directory)
        assert hp.state(directory) == "unpacked" and not hp.archives(directory)
    unreadable.chmod(0o600)
    unreadable.unlink()
    # the archive read back differs from what was written: refused, nothing removed
    monkeypatch.setattr(hp, "verify", lambda path, kind=None: ("0" * 64, 0))
    with pytest.raises(hp.PackError, match="not what was written"):
        hp.pack(directory)
    assert tree(home) == before and not list(directory.glob("*.tar.*")) and not list(directory.glob(".home*"))
    # a full disk while writing: the same
    monkeypatch.undo()
    if compressor == "zstd":
        monkeypatch.setattr(hp, "zstd", lambda: None)

    def full(*args, **kwargs):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(hp.tarfile.TarFile, "addfile", full)
    with pytest.raises(hp.PackError, match="No space left"):
        hp.pack(directory)
    assert tree(home) == before and not list(directory.glob(".home*"))


def test_an_unpack_that_fails_keeps_the_archive(tmp_path, compressor, monkeypatch):
    directory = tmp_path / "start"
    directory.mkdir()
    make_home(directory)
    record = hp.pack(directory)
    archive = directory / record["archive"]
    good = archive.read_bytes()
    # damaged: refused, the archive stays, no HOME is made
    archive.write_bytes(good[: len(good) // 2])
    with pytest.raises(hp.PackError, match="the archive stays|not unpacked"):
        hp.unpack(directory)
    assert hp.state(directory) == "packed" and not list(directory.glob(".home.unpacking-*"))
    # another digest than recorded: refused
    archive.write_bytes(good)
    hp.write_meta(directory, {**record, "digest": "f" * 64})
    with pytest.raises(hp.PackError, match="digest differs"):
        hp.unpack(directory)
    assert hp.state(directory) == "packed" and hp.meta(directory)["digest"] == "f" * 64
    # too little free space for it: refused before writing anything
    hp.write_meta(directory, {**record, "bytes": 1 << 60})
    with pytest.raises(hp.PackError, match="free"):
        hp.unpack(directory)
    hp.write_meta(directory, record)
    assert hp.unpack(directory) is not None and (directory / "home/work/notes.md").read_text() == "hello\n"


def test_unpack_refuses_entries_that_leave_the_home(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "zstd", lambda: None)
    directory = tmp_path / "start"
    directory.mkdir()
    import io
    import tarfile
    for bad in ("../escape", "/etc/evil", "a/../../b"):
        archive = directory / "home.tar.xz"
        with tarfile.open(archive, "w:xz") as tar:
            info = tarfile.TarInfo(bad)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
        hp.write_meta(directory, {"archive": "home.tar.xz", "entries": 1, "digest": "0"})
        with pytest.raises(hp.PackError, match="would leave"):
            hp.unpack(directory)
        assert not (tmp_path / "escape").exists() and not (directory / "home").exists()
    # a file below a link the archive made
    with tarfile.open(directory / "home.tar.xz", "w:xz") as tar:
        link = tarfile.TarInfo("up")
        link.type, link.linkname = tarfile.SYMTYPE, str(tmp_path)
        tar.addfile(link)
        info = tarfile.TarInfo("up/planted")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(hp.PackError, match="below a link"):
        hp.unpack(directory)
    assert not (tmp_path / "planted").exists()


def test_tidy_finishes_or_undoes_an_interrupted_pack_or_unpack(tmp_path, monkeypatch):
    monkeypatch.setattr(hp, "zstd", lambda: None)
    directory = tmp_path / "start"
    directory.mkdir()
    home = make_home(directory)
    before = tree(home)
    record = hp.pack(directory)
    archive = directory / record["archive"]
    # an unpack that stopped after the HOME was in place, before the archive went: the archive goes
    hp.unpack(directory)
    hp.pack(directory)
    meta = hp.meta(directory)
    real_unlink = Path.unlink

    def stop(self, *args, **kwargs):
        if self.name.startswith("home.tar"):
            raise KeyboardInterrupt
        return real_unlink(self, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", stop)
    with pytest.raises(KeyboardInterrupt):
        hp.unpack(directory)
    monkeypatch.undo()
    monkeypatch.setattr(hp, "zstd", lambda: None)
    assert home.is_dir() and archive.exists() and hp.meta(directory)["unpacked"] is True
    assert hp.packed(directory) is None and hp.tidy(directory) is True
    assert not archive.exists() and not (directory / hp.META).exists() and tree(home) == before
    # a pack that stopped right after moving the HOME aside (its archive there, no record yet): put back
    os.rename(home, directory / ".home.removing-99")
    (directory / "home.tar.xz").write_bytes(b"whole or not")
    assert hp.state(directory) == "none" and hp.tidy(directory)
    assert tree(home) == before and not hp.archives(directory)
    # leftovers aside go
    (directory / ".home.unpacking-5").mkdir()
    (directory / ".home.tar.5.partial").write_bytes(b"x")
    assert hp.tidy(directory) and not list(directory.glob(".home*"))
    # an empty HOME made beside a packed one (by something that did not unpack): removed, still packed
    hp.pack(directory)
    home.mkdir()
    assert hp.state(directory) == "both" and hp.tidy(directory) and hp.state(directory) == "packed"
    # a HOME with files beside a packed one: both kept, nothing unpacked over it
    home.mkdir()
    (home / "new").write_text("written meanwhile")
    assert not hp.tidy(directory) and hp.state(directory) == "both" and hp.unpack(directory) is None
    assert (home / "new").exists() and hp.archives(directory) and meta["digest"]
    with pytest.raises(hp.PackError, match="both"):
        hp.pack(directory)


def test_pack_refuses_what_is_not_a_home(tmp_path):
    directory = tmp_path / "start"
    directory.mkdir()
    with pytest.raises(hp.PackError, match="not a folder"):
        hp.pack(directory)
    (tmp_path / "elsewhere").mkdir()
    (directory / "home").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(hp.PackError, match="not a folder"):
        hp.pack(directory)
    assert hp.meta(directory) == {} and hp.packed(directory) is None
    (directory / hp.META).write_text("{broken")
    assert hp.meta(directory) == {}
    (directory / hp.META).write_text(json.dumps({"archive": "../../etc/passwd"}))
    assert hp.meta(directory) == {}
    assert gzip  # (the fake zstd's)
