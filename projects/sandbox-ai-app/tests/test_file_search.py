"""The file manager's search (file_search.py, user 2026-11-19: "eine generelle Suche mit Wildcard-Support"): the
patterns, the walk below a folder of this machine and of a remote system (the fake SFTP server: fake_sftp_server.py),
never through a link, nothing skipped silently, every limit said, Cancel. No Qt."""
import errno
import os
from pathlib import Path
import stat
import sys
import threading
import unicodedata

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox_ai_app import file_search, files, remote  # noqa: E402

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]


def matches(pattern, path, case=False, kind="file"):
    matcher = file_search.Matcher(pattern, case)
    return matcher.match(path, path.rsplit("/", 1)[-1], kind)


# -- patterns --

def test_a_pattern_without_a_slash_is_matched_against_the_name_at_any_depth():
    assert matches("*.txt", "a.txt") and matches("*.txt", "sub/deeper/c.txt")
    assert not matches("*.txt", "a.txt.bak") and not matches("*.txt", "sub.txt/x")
    assert matches("?.txt", "b.txt") and not matches("?.txt", "bb.txt")
    assert matches("[ab].txt", "a.txt") and not matches("[ab].txt", "c.txt")
    assert matches("[!ab].txt", "c.txt") and not matches("[!ab].txt", "a.txt")
    assert matches("[a-c]*", "beta") and not matches("[a-c]*", "delta")
    assert matches("", "anything/at/all"), "empty: every name"


def test_letter_case_is_ignored_unless_asked_and_nfd_names_match():
    assert matches("*.TXT", "Report.txt") and not matches("*.TXT", "Report.txt", case=True)
    assert matches("REPORT", "q3 report.txt"), "no wildcard: a part of the name, as the quick filter takes it"
    assert not matches("REPORT", "q3 report.txt", case=True) and matches("report", "q3 report.txt", case=True)
    nfd = unicodedata.normalize("NFD", "café.txt")
    assert matches("café*", nfd) and matches("CAFÉ*", nfd), "macOS's NFD names match a typed NFC pattern"


def test_a_slash_matches_the_path_below_the_start_and_double_star_any_depth():
    assert matches("src/*.py", "src/a.py") and not matches("src/*.py", "src/x/a.py")
    assert matches("src/**/*.py", "src/a.py") and matches("src/**/*.py", "src/x/y/a.py")
    assert not matches("src/**/*.py", "lib/src/a.py"), "anchored at the start folder"
    assert matches("**/b.txt", "b.txt") and matches("**/b.txt", "sub/b.txt")
    assert matches("/sub/*", "sub/b.txt"), "a leading slash is the start folder"
    assert matches("sub/**", "sub/deeper/c.txt")


def test_a_trailing_slash_takes_folders_only_and_a_broken_pattern_is_said():
    assert matches("build/", "x/build", kind="dir") and not matches("build/", "x/build", kind="file")
    with pytest.raises(ValueError, match="pattern"):
        file_search.Matcher("[z-a]", False)


# -- this machine --

@pytest.fixture
def tree(tmp_path):
    """start/ with files at three depths, a link to a folder outside that holds matching names, a link to a file."""
    start, outside = tmp_path / "start", tmp_path / "outside"
    (start / "sub" / "deeper").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("needle outside")
    (start / "a.txt").write_text("alpha needle")
    (start / "B.TXT").write_text("beta")
    (start / "run.sh").write_text("#!/bin/sh\necho needle\n")
    (start / "sub" / "b.txt").write_text("beta\nsecond line with Needle\n")
    (start / "sub" / "deeper" / "c.txt").write_text("gamma")
    (start / "sub" / "deeper" / "data.bin").write_bytes(b"needle\0binary")
    (start / "escape").symlink_to(outside)
    (start / "link.txt").symlink_to(outside / "secret.txt")
    return start


def search(root, relative="", pattern="", text="", case=False, **limits):
    found = file_search.Search(pattern, text, case, **limits)
    file_search.run(root, relative, found)
    return found


def paths(found):
    return sorted(hit.relative for hit in found.hits)


def test_every_entry_below_the_folder_and_links_are_named_never_followed(tree):
    root = files.Root(tree.parent, sandbox=True)
    found = search(root, "start", "*.txt")
    assert paths(found) == ["B.TXT", "a.txt", "link.txt", "sub/b.txt", "sub/deeper/c.txt"]
    assert "secret.txt" not in str(paths(found)) and found.links == 2, "escape/ and link.txt: shown, never followed"
    hit = next(hit for hit in found.hits if hit.relative == "sub/b.txt")
    assert hit.kind == "file" and hit.size == len("beta\nsecond line with Needle\n") and hit.mtime > 0
    assert next(hit for hit in found.hits if hit.relative == "link.txt").kind == "link"
    assert found.folders == 3 and found.done and found.stopped is None and found.unreadable == []
    assert [h.relative for h in search(root, "start", "escape").hits] == ["escape"], "the link itself is an entry"


def test_content_is_looked_for_in_text_files_only(tree):
    root = files.Root(tree)
    found = search(root, "", "", "NEEDLE")
    assert paths(found) == ["a.txt", "run.sh", "sub/b.txt"], "binary, links and the outside never"
    assert found.binary == 1 and found.looked_into >= 4
    hit = next(hit for hit in found.hits if hit.relative == "sub/b.txt")
    assert hit.line == 2 and "Needle" in hit.text
    assert paths(search(root, "", "", "NEEDLE", case=True)) == []
    assert paths(search(root, "", "*.sh", "needle")) == ["run.sh"], "both fields: names and contents"


def test_files_larger_than_the_cap_are_counted_not_read(tree):
    (tree / "big.txt").write_text("needle " * 100)
    found = search(files.Root(tree), "", "big.txt", "needle", content_limit=64)
    assert found.hits == [] and found.too_large == 1
    assert any("larger than" in line for line in found.summary())


def test_an_unreadable_folder_is_counted_and_named(tree, monkeypatch):
    (tree / "locked").mkdir()
    (tree / "locked" / "inside.txt").write_text("x")
    opened = files.Root._open_child_dir

    def refuse(self, parent, name):
        if name == "locked":
            raise PermissionError(errno.EACCES, "Permission denied")
        return opened(self, parent, name)
    monkeypatch.setattr(files.Root, "_open_child_dir", refuse)
    found = search(files.Root(tree), "", "*.txt")
    assert [rel for rel, _why in found.unreadable] == ["locked"] and "Permission denied" in found.unreadable[0][1]
    assert "sub/b.txt" in paths(found)
    assert any("could not be read" in line and "locked" in line for line in found.summary())


def test_a_mounted_folder_is_named_and_not_entered(tree, monkeypatch):
    real = files._mount_of
    monkeypatch.setattr(file_search, "mount_of", lambda fd: ("mount", 99) if files.fd_location(fd).endswith("/sub")
                        else real(fd))
    found = search(files.Root(tree), "", "*.txt")
    assert "sub/b.txt" not in paths(found) and [rel for rel, _ in found.not_entered] == ["sub"]
    assert any("not searched" in line for line in found.summary())


def test_results_depth_entries_and_time_are_bounded_and_said(tree):
    root = files.Root(tree)
    found = search(root, "", "*", max_results=2)
    assert len(found.hits) == 2 and found.stopped == "results"
    assert any("2 results" in line for line in found.summary())
    found = search(root, "", "*.txt", max_depth=1)
    assert "sub/deeper/c.txt" not in paths(found) and "sub/b.txt" in paths(found)
    assert [rel for rel, _ in found.not_entered] == ["sub/deeper"]
    found = search(root, "", "*", max_entries=3)
    assert found.stopped == "entries" and any("entries" in line for line in found.summary())
    ticks = iter(range(0, 10_000, 50))
    found = file_search.Search("*", seconds=100, clock=lambda: next(ticks))
    file_search.run(root, "", found)
    assert found.stopped == "time" and any("100 s" in line for line in found.summary())


def test_cancel_stops_it(tree):
    calls = []
    found = file_search.Search("*", cancelled=lambda: calls.append(1) or len(calls) > 3)
    file_search.run(files.Root(tree), "", found)
    assert found.stopped == "cancelled" and found.done


def test_a_start_folder_that_is_a_link_is_refused(tree):
    with pytest.raises(files.Refused):
        search(files.Root(tree), "escape", "*")


# -- a remote system --

def connect(path, **env):
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        return remote.RemoteRoot(remote.Target("fake"), str(path), ssh=FAKE, timeout=10)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_a_remote_folder_gives_what_a_local_one_gives(tree):
    root = connect(tree.parent)
    try:
        for pattern, text in (("*.txt", ""), ("", "needle"), ("sub/**", ""), ("escape", ""), ("**/c.txt", "")):
            local = search(files.Root(tree.parent), "start", pattern, text)
            there = search(root, "start", pattern, text)
            assert paths(there) == paths(local), (pattern, text)
            assert there.links == local.links and there.binary == local.binary
        assert root.list("start")[0].name == "sub", "the connection is fine afterwards"
    finally:
        root.close()


def test_a_remote_walk_pipelines_its_requests(tmp_path):
    for i in range(40):
        (tmp_path / "t" / f"d{i:02}" / "inner").mkdir(parents=True)
        (tmp_path / "t" / f"d{i:02}" / "inner" / "x.txt").write_text(f"file {i}")
    log = tmp_path / "log"
    root = connect(tmp_path, FAKE_SFTP_LOG=str(log))
    try:
        found = file_search.Search("x.txt", "file 3")
        scans = []
        real = remote.TreeScan.run

        def kept(self):
            scans.append(self)
            return real(self)
        remote.TreeScan.run = kept
        try:
            file_search.run(root, "t", found)
        finally:
            remote.TreeScan.run = real
        assert sorted(h.relative for h in found.hits) == ["d03/inner/x.txt", *(f"d{i}/inner/x.txt" for i in range(30, 40))]
        assert scans[0].most >= 16, "many requests on their way at once, not one round trip a folder"
        assert scans[0].handles == set(), "every folder and file handle closed"
        requests = log.read_text().splitlines()
        assert sum(1 for line in requests if line.startswith("OPENDIR")) == 81
        assert not any(line.startswith(("WRITE", "REMOVE", "SETSTAT", "MKDIR", "RENAME")) for line in requests)
    finally:
        root.close()


def test_a_remote_link_is_never_entered_even_where_the_server_follows_it(tree):
    (tree / "sub" / "back").symlink_to(tree.parent / "outside")
    root = connect(tree.parent, FAKE_SFTP_FOLLOW_IN_LISTINGS="1")
    try:
        found = search(root, "start", "", "needle")
        assert paths(found) == ["a.txt", "run.sh", "sub/b.txt"], "no file behind a link read"
        assert not any("secret" in hit.relative for hit in search(root, "start", "*").hits)
    finally:
        root.close()


def test_a_remote_unreadable_folder_and_short_reads(tree):
    (tree / "locked").mkdir()
    (tree / "locked" / "a.txt").write_text("needle")
    (tree / "long.txt").write_text("x" * 5000 + " needle")
    root = connect(tree.parent, FAKE_SFTP_DENY="locked", FAKE_SFTP_SHORT_READS="1")
    try:
        found = search(root, "start", "", "needle")
        assert [rel for rel, _ in found.unreadable] == ["locked"] and "denied" in found.unreadable[0][1].lower()
        assert "long.txt" in paths(found), "a short answer is no end of the file"
    finally:
        root.close()


def test_a_remote_search_stopped_early_leaves_the_connection_usable(tmp_path):
    for i in range(30):
        (tmp_path / "t" / f"d{i:02}").mkdir(parents=True)
        (tmp_path / "t" / f"d{i:02}" / "x.txt").write_text("needle")
    root = connect(tmp_path)
    try:
        found = search(root, "t", "*.txt", max_results=3)
        assert len(found.hits) == 3 and found.stopped == "results"
        found = search(root, "t", "", "needle", max_results=2)
        assert len(found.hits) == 2 and found.stopped == "results"
        calls = []
        found = file_search.Search("*", cancelled=lambda: calls.append(1) or len(calls) > 5)
        file_search.run(root, "t", found)
        assert found.stopped == "cancelled"
        assert len(root.list("t")) == 30 and root.read_file("t/d00/x.txt").data == b"needle"
    finally:
        root.close()


def test_a_remote_search_lets_a_listing_go_between(tmp_path):
    """The session's lock is held a slice at a time: a pane's listing is not held up by a long search."""
    for i in range(300):
        (tmp_path / "t" / f"d{i:03}" / "e").mkdir(parents=True)
    root = connect(tmp_path)
    try:
        found = file_search.Search("nothing-matches")
        thread = threading.Thread(target=file_search.run, args=(root, "t", found))
        thread.start()
        listed = root.list("t")
        assert len(listed) == 300
        thread.join(60)
        assert found.done and found.folders == 601
    finally:
        root.close()


def test_against_openssh_sftp_server(tree):
    from test_remote import sftp_server
    server = sftp_server()
    if server is None:
        pytest.skip("OpenSSH's sftp-server is not installed (openssh-sftp-server, or $SANDBOX_AI_TEST_SFTP_SERVER)")
    root = remote.RemoteRoot(remote.Target("fake"), str(tree.parent), ssh=[server, "-e"], timeout=10)
    try:
        assert paths(search(root, "start", "*.txt")) == paths(search(files.Root(tree.parent), "start", "*.txt"))
        assert paths(search(root, "start", "", "needle")) == ["a.txt", "run.sh", "sub/b.txt"]
    finally:
        root.close()


def test_mode_bits_are_kept_for_the_kind():
    hit = file_search.Hit("x", "file", 1, 0.0, stat.S_IFREG | 0o644)
    assert hit.kind == "file" and stat.S_ISREG(hit.mode)
