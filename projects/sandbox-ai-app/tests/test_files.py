"""The file browser's file access: never through a symbolic link, never above the root."""
import errno
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox_ai_app import files  # noqa: E402
from sandbox_ai_app.files import Refused  # noqa: E402


@pytest.fixture
def tree(tmp_path):
    """root/ with files, a folder, links pointing out and in; outside/ next to it with a secret."""
    root, outside, host = tmp_path / "root", tmp_path / "outside", tmp_path / "host"
    for path in (root, outside, host):
        path.mkdir()
    (outside / "secret.txt").write_text("host secret")
    (root / "a.txt").write_text("alpha")
    (root / "run.sh").write_text("#!/bin/sh\n")
    (root / "run.sh").chmod(0o755)
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("beta")
    (root / "sub" / "deeper").mkdir()
    (root / "sub" / "deeper" / "c.txt").write_text("gamma")
    (root / "escape").symlink_to(outside)
    (root / "secret-link").symlink_to(outside / "secret.txt")
    (root / "sub" / "up").symlink_to("../..")
    (root / "sub" / "inner-link").symlink_to("b.txt")
    return files.Root(root), root, outside, host


def test_parts_refuse_dotdot_absolute_and_nul():
    assert files.parts("") == [] and files.parts("./a//b/") == ["a", "b"]
    for bad in ("..", "a/../b", "/etc", "a/\0b", "sub/.."):
        with pytest.raises(Refused):
            files.parts(bad)


def test_listing_shows_links_without_following(tree):
    root, *_ = tree
    entries = {e.name: e for e in root.list()}
    assert [e.name for e in root.list()][:1] == ["sub"]  # folders first
    assert entries["escape"].kind == "link" and entries["escape"].target.endswith("outside")
    assert entries["secret-link"].kind == "link" and entries["a.txt"].kind == "file" and entries["a.txt"].size == 5
    sub = {e.name: e for e in root.list("sub")}
    assert sub["up"].kind == "link" and sub["up"].target == "../.." and sub["deeper"].kind == "dir"


@pytest.mark.parametrize("path", ["escape", "escape/", "sub/up", "sub/up/outside", "secret-link", "a.txt"])
def test_no_listing_through_links_or_files(tree, path):
    root, *_ = tree
    with pytest.raises(Refused):
        root.list(path)


@pytest.mark.parametrize("path", ["..", "../outside", "sub/../../outside", "/", "/etc"])
def test_no_listing_above_the_root(tree, path):
    root, *_ = tree
    with pytest.raises(Refused):
        root.list(path)


def test_copy_out_file_and_folder(tree):
    root, _, _, host = tree
    result = root.copy_out("a.txt", host)
    assert (host / "a.txt").read_text() == "alpha" and result.files == 1 and result.bytes == 5
    result = root.copy_out("run.sh", host)
    assert not os.access(host / "run.sh", os.X_OK), "nothing the agent wrote becomes executable on the host"
    assert (host / "run.sh").stat().st_mode & 0o111 == 0 and result.executable == ["run.sh"]
    result = root.copy_out("sub", host)
    assert (host / "sub" / "b.txt").read_text() == "beta" and (host / "sub" / "deeper" / "c.txt").read_text() == "gamma"
    assert sorted(p for p, _ in result.skipped) == ["sub/inner-link", "sub/up"]
    assert not (host / "sub" / "up").exists() and not (host / "sub" / "up").is_symlink()
    assert result.files == 2 and result.directories == 2


def test_copy_out_refuses_links_and_existing_targets(tree):
    root, _, _, host = tree
    for path in ("secret-link", "escape", "escape/secret.txt", "sub/up/outside/secret.txt", "../outside/secret.txt"):
        with pytest.raises(Refused):
            root.copy_out(path, host)
    assert list(host.iterdir()) == []
    root.copy_out("a.txt", host)
    (host / "a.txt").write_text("mine")
    with pytest.raises(Refused):
        root.copy_out("a.txt", host)
    assert (host / "a.txt").read_text() == "mine"


def test_copy_out_never_writes_through_a_link_on_the_host(tree):
    root, _, outside, host = tree
    (host / "a.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(Refused):
        root.copy_out("a.txt", host)
    assert (outside / "secret.txt").read_text() == "host secret"


def test_whole_root_copy(tree):
    root, path, _, host = tree
    result = root.copy_out("", host)
    assert (host / path.name / "a.txt").read_text() == "alpha"
    assert {p for p, _ in result.skipped} >= {"root/escape", "root/secret-link"}
    assert not (host / path.name / "escape").exists()


def test_fifo_and_devices_are_refused_without_hanging(tree):
    root, path, _, host = tree
    os.mkfifo(path / "pipe")
    done = []
    worker = threading.Thread(target=lambda: done.append(pytest.raises(Refused, root.copy_out, "pipe", host)))
    worker.start()
    worker.join(5)
    assert done, "copying a FIFO must not block"
    os.mkdir(host / "all")
    result = root.copy_out("", host / "all")
    assert ("root/pipe", "not a regular file") in result.skipped


def test_size_and_entry_limits_remove_partial_copies(tree):
    root, path, _, host = tree
    (path / "big.bin").write_bytes(b"x" * 5000)
    with pytest.raises(Refused):
        root.copy_out("big.bin", host, limit=4096)
    assert not (host / "big.bin").exists()
    with pytest.raises(Refused):
        root.copy_out("sub", host, entries=2)
    assert not (host / "sub").exists()


def test_copy_in_file_folder_and_overwrite(tree, tmp_path):
    root, path, _, _ = tree
    upload = tmp_path / "upload"
    upload.mkdir()
    (upload / "new.txt").write_text("new")
    (upload / "folder").mkdir()
    (upload / "folder" / "x.txt").write_text("x")
    (upload / "folder" / "link").symlink_to("/etc/passwd")
    root.copy_in(upload / "new.txt", "sub")
    assert (path / "sub" / "new.txt").read_text() == "new"
    with pytest.raises(Refused):
        root.copy_in(upload / "new.txt", "sub")
    (upload / "new.txt").write_text("newer")
    root.copy_in(upload / "new.txt", "sub", overwrite=True)
    assert (path / "sub" / "new.txt").read_text() == "newer"
    result = root.copy_in(upload / "folder")
    assert (path / "folder" / "x.txt").read_text() == "x" and result.skipped == [("folder/link", "symbolic link")]
    assert not (path / "folder" / "link").is_symlink()
    with pytest.raises(Refused):
        root.copy_in(upload / "folder" / "link")


def test_copy_in_never_writes_through_links_in_the_root(tree, tmp_path):
    root, path, outside, _ = tree
    upload = tmp_path / "secret.txt"
    upload.write_text("overwritten?")
    # Into a folder that is a link: refused.
    for folder in ("escape", "sub/up/outside"):
        with pytest.raises(Refused):
            root.copy_in(upload, folder)
    # Onto a name that is a link to a host file: without overwrite refused, with overwrite the link is replaced.
    (path / "secret.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(Refused):
        root.copy_in(upload, "")
    root.copy_in(upload, "", overwrite=True)
    assert (outside / "secret.txt").read_text() == "host secret"
    assert not (path / "secret.txt").is_symlink() and (path / "secret.txt").read_text() == "overwritten?"
    # A hard link to a file elsewhere is replaced too, not written through.
    (path / "hard.txt").unlink(missing_ok=True)
    os.link(outside / "secret.txt", path / "hard.txt")
    upload2 = tmp_path / "hard.txt"
    upload2.write_text("new content")
    root.copy_in(upload2, "", overwrite=True)
    assert (outside / "secret.txt").read_text() == "host secret"


def test_copy_in_refuses_a_link_as_source(tree, tmp_path):
    root, *_ = tree
    (tmp_path / "link-to-secret").symlink_to(tmp_path / "outside" / "secret.txt")
    with pytest.raises(Refused):
        root.copy_in(tmp_path / "link-to-secret")


def test_race_folder_swapped_for_a_link_during_the_walk(tree):
    """The agent replaces sub/deeper with a link to the outside between two steps of the walk."""
    root, path, outside, host = tree

    def swap(fd, part):
        if part == "deeper":
            os.rename(path / "sub" / "deeper", path / "moved")
            os.symlink(outside, path / "sub" / "deeper")
            root.hook = None
    root.hook = swap
    with pytest.raises(Refused):
        root.copy_out("sub/deeper/secret.txt", host)
    assert list(host.iterdir()) == []


def test_race_folder_moved_out_of_the_root_is_noticed(tree, tmp_path):
    """A folder that was opened and then moved outside the root is refused by the /proc check."""
    if not Path("/proc/self/fd").is_dir():
        pytest.skip("needs /proc")
    root, path, _, host = tree
    elsewhere = tmp_path / "elsewhere"

    def move(fd, part):
        if part == "deeper":  # sub is open now; move it out of the root
            os.rename(path / "sub", elsewhere)
            root.hook = None
    root.hook = move
    with pytest.raises(Refused):
        root.list("sub/deeper")


def test_race_file_swapped_for_a_link_before_the_copy(tree):
    root, path, outside, host = tree
    real_open = files._open_regular

    def swapped(name, dir_fd=None):
        if name == "a.txt":
            os.unlink(path / "a.txt")
            os.symlink(outside / "secret.txt", path / "a.txt")
        return real_open(name, dir_fd)
    files._open_regular = swapped
    try:
        with pytest.raises(Refused):
            root.copy_out("a.txt", host)
    finally:
        files._open_regular = real_open
    assert not (host / "a.txt").exists()


def test_read_preview_and_printable(tree):
    root, path, *_ = tree
    assert root.read("sub/b.txt") == b"beta"
    with pytest.raises(Refused):
        root.read("secret-link")
    assert files.printable("a\nb\x1b[31m") == "a\\x0ab\\x1b[31m"
    assert files.size_text(512) == "512 B" and files.size_text(2048) == "2 KiB"


# -- copies cut short never leave a truncated file under the real name --


def big(path, size=3 * files.CHUNK + 5):
    path.write_bytes(b"x" * size)
    return size


def leftovers(directory):
    return sorted(p.name for p in directory.iterdir())


def test_a_cancelled_copy_leaves_no_file_under_its_name(tree):
    root, path, _, host = tree
    big(path / "big.bin")
    calls = []

    def cancelled():  # after the first chunk
        calls.append(1)
        return len(calls) > 2
    with pytest.raises(Refused):
        root.copy_out("big.bin", host, cancelled=cancelled)
    assert leftovers(host) == [], "neither big.bin nor its temporary file"
    target = path / "inbox"
    target.mkdir()
    calls.clear()
    with pytest.raises(Refused):
        root.copy_in(str(path / "big.bin"), "inbox", cancelled=cancelled)
    assert leftovers(target) == []


def test_a_name_that_appears_during_the_copy_is_not_replaced(tree, monkeypatch):
    root, path, _, host = tree
    big(path / "big.bin")
    real = files._copy_data

    def copy_and_plant(source, target, budget):
        real(source, target, budget)
        (host / "big.bin").write_text("planted meanwhile")
    monkeypatch.setattr(files, "_copy_data", copy_and_plant)
    with pytest.raises(Refused, match="already exists"):
        root.copy_out("big.bin", host)
    assert (host / "big.bin").read_text() == "planted meanwhile" and leftovers(host) == ["big.bin"]


def test_the_temporary_file_of_a_copy_running_at_exit_is_removed(tmp_path):
    """The interpreter exits while a copy runs in a daemon thread: the atexit handler removes its file."""
    workspace, host = tmp_path / "ws", tmp_path / "host"
    workspace.mkdir()
    host.mkdir()
    big(workspace / "big.bin")
    script = f"""
import sys, threading, time
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from sandbox_ai_app import files
real = files._copy_data
def slow(source, target, budget):
    import os
    os.write(target, b"partial")
    time.sleep(60)
files._copy_data = slow
root = files.Root({str(workspace)!r})
threading.Thread(target=root.copy_out, args=("big.bin", {str(host)!r}), daemon=True).start()
import os
deadline = time.monotonic() + 20
while not any(n.endswith(".part") for n in os.listdir({str(host)!r})) and time.monotonic() < deadline:
    time.sleep(0.01)
print("parts:", [n for n in os.listdir({str(host)!r}) if n.endswith(".part")])
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert "parts: ['.sandbox-ai-app-" in result.stdout, result.stdout + result.stderr
    assert leftovers(host) == [], "a truncated big.bin or its temporary file survived the exit"


def test_a_cancelled_folder_copy_removes_the_folders_it_created_and_only_those(tree, tmp_path):
    root, path, _, host = tree
    for name in ("one", "two", "three"):
        big(path / "sub" / f"{name}.bin", files.CHUNK + 1)
    watched = []

    def cancelled():  # once the first file of the folder is complete under its name
        folder = watched[0]
        return folder.is_dir() and any(not n.startswith(".") and n.endswith(".bin") for n in os.listdir(folder))
    watched[:] = [host / "sub"]
    with pytest.raises(Refused):
        root.copy_out("sub", host, cancelled=cancelled)
    assert leftovers(host) == [], "no partial sub/ with only some of its files"
    # Into a folder that exists (replace): what was there stays, the new subfolder goes.
    inbox = path / "inbox"
    (inbox / "sub").mkdir(parents=True)
    (inbox / "sub" / "mine.txt").write_text("was here before")
    source = tmp_path / "from-host" / "sub"
    (source / "new-folder").mkdir(parents=True)
    for name in ("a", "b", "c"):
        big(source / "new-folder" / f"{name}.bin", files.CHUNK + 1)
    watched[:] = [inbox / "sub" / "new-folder"]
    with pytest.raises(Refused):
        root.copy_in(str(source), "inbox", overwrite=True, cancelled=cancelled)
    assert leftovers(inbox / "sub") == ["mine.txt"] and (inbox / "sub" / "mine.txt").read_text() == "was here before"


def test_the_folders_of_a_copy_running_at_exit_are_removed(tmp_path):
    workspace, host = tmp_path / "ws", tmp_path / "host"
    (workspace / "tree" / "deeper").mkdir(parents=True)
    (workspace / "tree" / "a.txt").write_text("a")
    big(workspace / "tree" / "deeper" / "big.bin")
    (host / "kept").mkdir(parents=True)
    script = f"""
import os, sys, threading, time
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from sandbox_ai_app import files
real = files._copy_data
def slow(source, target, budget):
    if os.fstat(source).st_size > 100:
        time.sleep(60)
    real(source, target, budget)
files._copy_data = slow
root = files.Root({str(workspace)!r})
threading.Thread(target=root.copy_out, args=("tree", {str(host)!r}), daemon=True).start()
deadline = time.monotonic() + 20
while not os.path.exists({str(host / "tree" / "a.txt")!r}) and time.monotonic() < deadline:
    time.sleep(0.01)
time.sleep(0.2)
print("partial:", sorted(os.listdir({str(host / "tree")!r})))
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60)
    assert "partial: ['a.txt', 'deeper']" in result.stdout, result.stdout + result.stderr
    assert leftovers(host) == ["kept"], "the partial tree is gone; what was there before stays"


def test_without_working_hard_links_a_copy_is_checked_then_renamed(tree, monkeypatch):
    root, _, _, host = tree

    def no_links(*args, **kwargs):
        raise OSError(errno.EIO, "Input/output error")  # what some FUSE file systems answer
    monkeypatch.setattr(files.os, "link", no_links)
    root.copy_out("a.txt", host)
    assert (host / "a.txt").read_text() == "alpha" and leftovers(host) == ["a.txt"]
    with pytest.raises(Refused, match="already exists"):
        root.copy_out("a.txt", host)
    assert leftovers(host) == ["a.txt"]


def test_a_copy_swapped_for_a_link_while_it_is_written_never_links_a_host_file(tree, monkeypatch):
    """Copying into the workspace writes a temporary file there, which the agent can swap for a symlink to a host
    file before it gets its name: the hard link must not follow it (the agent would get the host file itself, to
    read and to write), and the swapped copy is refused."""
    root, path, outside, host = tree
    (host / "upload.txt").write_text("from the host")
    secret = outside / "secret.txt"
    links_before = secret.stat().st_nlink
    real = files._copy_data

    def copy_and_swap(source, target, budget):
        real(source, target, budget)
        for name in os.listdir(path):
            if name.startswith(".") and name not in (".", ".."):  # the temporary file, next to the target
                os.unlink(path / name)
                os.symlink(secret, path / name)
    monkeypatch.setattr(files, "_copy_data", copy_and_swap)
    with pytest.raises(Refused, match="replaced while it was written"):
        root.copy_in(host / "upload.txt", "")
    assert secret.stat().st_nlink == links_before, "no hard link to the host file"
    assert not (path / "upload.txt").exists()
