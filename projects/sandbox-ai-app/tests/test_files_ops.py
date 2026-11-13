"""The two-pane file manager's operations (files.py, no Qt): copy and move between a sandbox root and the host,
delete, rename, new folder, chmod, chown to oneself and taking files back from a rootless Podman sandbox - never
through a symbolic link, never above the root, within budgets, cancellable, nothing half-done left behind.
Everything happens below tmp_path (the host side is Root("/") walked down to tmp_path)."""
import os
from pathlib import Path
import stat
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sandbox_ai_app import files  # noqa: E402
from sandbox_ai_app.files import Cancelled, Refused  # noqa: E402



def can_give_away(uid):
    """True if this process may give a file to UID (root with CAP_CHOWN, and UID mapped in its user namespace)."""
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        probe = Path(folder) / "probe"
        probe.write_text("")
        try:
            os.chown(probe, uid, uid)
            return True
        except OSError:
            return False


GIVE_AWAY = can_give_away(12345)
SUBORDINATE = can_give_away(100999) and can_give_away(100007)


def leftovers(path):
    return sorted(str(p.relative_to(path)) for p in Path(path).rglob("*") if p.name.startswith(".sandbox-ai-app-"))


@pytest.fixture
def places(tmp_path):
    """A sandbox root ws/ (files, a folder, links pointing out), the host Root("/") with its folder host/ below
    tmp_path, and outside/ with a file the agent's links point at."""
    ws, host, outside = tmp_path / "ws", tmp_path / "host", tmp_path / "outside"
    for path in (ws, host, outside):
        path.mkdir()
    (outside / "precious.txt").write_text("the host's own")
    (ws / "a.txt").write_text("alpha")
    (ws / "run.sh").write_text("#!/bin/sh\n")
    (ws / "run.sh").chmod(0o755)
    (ws / "sub" / "deeper").mkdir(parents=True)
    (ws / "sub" / "b.txt").write_text("beta")
    (ws / "sub" / "deeper" / "c.txt").write_text("gamma")
    (ws / "escape").symlink_to(outside)
    (ws / "planted").symlink_to(outside / "precious.txt")
    sandbox = files.Root(ws)
    machine = files.Root("/", label="host", sandbox=False)
    return sandbox, machine, ws, host, outside


def rel(path):
    """PATH relative to the host Root("/")."""
    return os.path.realpath(path).lstrip("/")


# -- copy, both ways --

def test_copy_out_of_the_sandbox_drops_exec_bits_and_names_risky_files(places):
    sandbox, machine, ws, host, _ = places
    (ws / "sub" / ".envrc").write_text("export X=1\n")
    result = files.copy_entry(sandbox, "run.sh", machine, rel(host))
    assert (host / "run.sh").read_text() == "#!/bin/sh\n" and not os.access(host / "run.sh", os.X_OK)
    assert result.executable == ["run.sh"]
    result = files.copy_entry(sandbox, "sub", machine, rel(host))
    assert (host / "sub" / "deeper" / "c.txt").read_text() == "gamma"
    assert result.files == 3 and result.directories == 2 and result.risky == ["sub/.envrc"]
    assert leftovers(host) == []


def test_copy_into_the_sandbox_keeps_exec_bits_but_never_set_id_bits(places, tmp_path):
    sandbox, machine, ws, host, _ = places
    tool = host / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755 | stat.S_ISUID)
    (host / "folder" / "inner").mkdir(parents=True)
    (host / "folder" / "inner" / "x").write_text("x")
    (host / "folder" / "link").symlink_to(tmp_path / "outside")
    result = files.copy_entry(machine, rel(tool), sandbox, "sub")
    mode = (ws / "sub" / "tool").stat().st_mode
    assert mode & 0o100 and not mode & files.SPECIAL_BITS and result.executable == []
    result = files.copy_entry(machine, rel(host / "folder"), sandbox, "")
    assert (ws / "folder" / "inner" / "x").read_text() == "x" and not (ws / "folder" / "link").exists()
    assert [path for path, _ in result.skipped] == ["folder/link"]


def test_a_private_file_or_folder_stays_private_in_its_copy(places):
    """Review: every copy was made 0o666/0o777 less the umask - a key of 0600 in a folder of 0700 became 0644 in
    0755, readable by everyone on the machine. A copy keeps the permission bits (the umask on top), its owner may
    read and write it, and what comes out of a sandbox is never executable."""
    sandbox, machine, ws, host, _ = places
    old = os.umask(0o022)
    try:
        keys = host / "keys"
        keys.mkdir(mode=0o700)
        (keys / "id").write_text("secret")
        (keys / "id").chmod(0o600)
        (keys / "id.pub").write_text("public")
        (keys / "id.pub").chmod(0o444)
        target = host / "elsewhere"
        target.mkdir()
        files.copy_entry(machine, rel(keys), machine, rel(target))  # from one host folder to another
        files.copy_entry(machine, rel(keys), sandbox, "")  # into the sandbox
        for copied in (target / "keys", ws / "keys"):
            assert stat.S_IMODE(copied.stat().st_mode) == 0o700
            assert stat.S_IMODE((copied / "id").stat().st_mode) == 0o600
            assert stat.S_IMODE((copied / "id.pub").stat().st_mode) == 0o644
        (ws / "private").write_text("the agent's")
        (ws / "private").chmod(0o700)
        files.copy_entry(sandbox, "private", machine, rel(host))  # out of the sandbox: never executable
        assert stat.S_IMODE((host / "private").stat().st_mode) == 0o600
    finally:
        os.umask(old)


def test_a_copy_within_one_root_under_another_name(places):
    sandbox, _, ws, _, _ = places
    files.copy_entry(sandbox, "sub", sandbox, "", name="sub-copy")
    assert (ws / "sub-copy" / "deeper" / "c.txt").read_text() == "gamma"
    with pytest.raises(Refused, match="same folder"):
        files.copy_entry(sandbox, "a.txt", sandbox, "")


def test_a_folder_is_never_copied_or_moved_into_itself(places):
    sandbox, _, ws, _, _ = places
    with pytest.raises(Refused, match="into itself"):
        files.copy_entry(sandbox, "sub", sandbox, "sub/deeper")
    with pytest.raises(Refused, match="into itself"):
        files.move_entry(sandbox, "sub", sandbox, "sub/deeper")
    assert sorted(p.name for p in (ws / "sub" / "deeper").iterdir()) == ["c.txt"]


def test_overwrite_is_refused_unless_asked_and_never_writes_through_a_link(places):
    sandbox, machine, ws, host, outside = places
    (host / "a.txt").write_text("host version")
    with pytest.raises(Refused, match="already exists"):
        files.copy_entry(sandbox, "a.txt", machine, rel(host))
    with pytest.raises(Refused, match="already exists"):
        files.move_entry(sandbox, "a.txt", machine, rel(host))
    assert (host / "a.txt").read_text() == "host version" and (ws / "a.txt").exists()
    files.copy_entry(sandbox, "a.txt", machine, rel(host), overwrite=True)
    assert (host / "a.txt").read_text() == "alpha"
    # Into the sandbox: the agent put a link where the copy goes; it is replaced, never written through.
    (host / "planted").write_text("from the host")
    with pytest.raises(Refused, match="already exists"):
        files.copy_entry(machine, rel(host / "planted"), sandbox, "")
    files.copy_entry(machine, rel(host / "planted"), sandbox, "", overwrite=True)
    assert (outside / "precious.txt").read_text() == "the host's own"
    assert not (ws / "planted").is_symlink() and (ws / "planted").read_text() == "from the host"


# -- links and '..' --

OPERATIONS = {
    "copy out": lambda s, m, host, path: files.copy_entry(s, path, m, rel(host)),
    "copy in to": lambda s, m, host, path: files.copy_entry(m, rel(host / "h.txt"), s, path),
    "move out": lambda s, m, host, path: files.move_entry(s, path, m, rel(host)),
    "move in to": lambda s, m, host, path: files.move_entry(m, rel(host / "h.txt"), s, path),
    "delete": lambda s, m, host, path: s.delete_entry(path),
    "rename": lambda s, m, host, path: s.rename_entry(path, "renamed"),
    "mkdir in": lambda s, m, host, path: s.make_folder(path, "new"),
    "chmod": lambda s, m, host, path: s.chmod_entry(path, 0o600),
    "chown": lambda s, m, host, path: s.chown_entry(path, os.getuid(), os.getgid(), recursive=True),
    "take ownership": lambda s, m, host, path: files.take_ownership(s, path, podman="/nonexistent/podman"),
}


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
@pytest.mark.parametrize("path", ["escape/precious.txt", "escape/x/y", "../outside/precious.txt",
                                  "sub/../../outside/precious.txt", "/etc/passwd", "sub/\0x"])
def test_no_operation_goes_through_a_link_in_the_middle_or_above_the_root(places, operation, path):
    sandbox, machine, ws, host, outside = places
    (host / "h.txt").write_text("h")
    before = sorted(str(p.relative_to(outside)) for p in outside.rglob("*"))
    with pytest.raises(Refused):
        OPERATIONS[operation](sandbox, machine, host, path)
    assert sorted(str(p.relative_to(outside)) for p in outside.rglob("*")) == before
    assert (outside / "precious.txt").read_text() == "the host's own"
    assert (host / "h.txt").exists(), "a refused move keeps its source"


@pytest.mark.parametrize("operation", ["copy out", "move out", "chmod", "take ownership"])
def test_a_link_as_the_entry_itself_is_refused(places, operation):
    sandbox, machine, _, host, outside = places
    mode = (outside / "precious.txt").stat().st_mode
    with pytest.raises(Refused, match="symbolic link"):
        OPERATIONS[operation](sandbox, machine, host, "planted")
    assert (outside / "precious.txt").stat().st_mode == mode and list(host.iterdir()) == []


def test_a_link_as_the_target_folder_is_refused(places):
    sandbox, machine, ws, host, outside = places
    (host / "h.txt").write_text("h")
    for target in ("escape", "planted"):
        with pytest.raises(Refused):
            files.copy_entry(machine, rel(host / "h.txt"), sandbox, target)
        with pytest.raises(Refused):
            files.move_entry(machine, rel(host / "h.txt"), sandbox, target)
        with pytest.raises(Refused):
            sandbox.make_folder(target, "new")
    assert sorted(p.name for p in outside.iterdir()) == ["precious.txt"] and (host / "h.txt").exists()


def test_links_are_deleted_renamed_and_chowned_themselves_never_what_they_point_to(places):
    sandbox, _, ws, _, outside = places
    sandbox.chown_entry("planted", os.getuid(), os.getgid())
    sandbox.rename_entry("planted", "moved-link")
    assert (ws / "moved-link").is_symlink() and (outside / "precious.txt").read_text() == "the host's own"
    result = sandbox.delete_entry("moved-link")
    assert result.removed == 1 and not (ws / "moved-link").is_symlink() and (outside / "precious.txt").exists()
    result = sandbox.delete_entry("escape")
    assert result.removed == 1 and (outside / "precious.txt").exists()


@pytest.mark.skipif(not GIVE_AWAY, reason="giving a file away needs root")
def test_chown_never_changes_what_a_link_points_to(places):
    sandbox, _, ws, _, outside = places
    os.chown(outside / "precious.txt", 12345, 12345)
    (ws / "sub" / "to-precious").symlink_to(outside / "precious.txt")
    sandbox.chown_entry("sub", os.getuid(), os.getgid(), recursive=True)
    assert (outside / "precious.txt").stat().st_uid == 12345


# -- move --

def test_move_between_roots_copies_then_removes_the_source(places):
    sandbox, machine, ws, host, _ = places
    (ws / "sub" / "deeper" / "tool").write_text("#!/bin/sh\n")
    (ws / "sub" / "deeper" / "tool").chmod(0o755)
    result = files.move_entry(sandbox, "sub/deeper", machine, rel(host))
    assert result.moved and not (ws / "sub" / "deeper").exists() and (host / "deeper" / "c.txt").read_text() == "gamma"
    assert result.executable == ["deeper/tool"] and not os.access(host / "deeper" / "tool", os.X_OK)
    result = files.move_entry(machine, rel(host / "deeper"), sandbox, "")
    assert result.moved and not (host / "deeper").exists() and (ws / "deeper" / "c.txt").exists()


def test_move_within_a_root_renames(places, monkeypatch):
    sandbox, _, ws, _, _ = places
    inode = (ws / "sub").stat().st_ino
    monkeypatch.setattr(files, "copy_entry", lambda *a, **k: pytest.fail("a move within one root is a rename"))
    sandbox.make_folder("", "into")
    result = files.move_entry(sandbox, "sub", sandbox, "into")
    assert result.moved and (ws / "into" / "sub").stat().st_ino == inode and not (ws / "sub").exists()
    (ws / "into" / "a.txt").write_text("old")
    with pytest.raises(Refused, match="already exists"):
        files.move_entry(sandbox, "a.txt", sandbox, "into")
    files.move_entry(sandbox, "a.txt", sandbox, "into", overwrite=True)
    assert (ws / "into" / "a.txt").read_text() == "alpha" and not (ws / "a.txt").exists()


def test_move_keeps_the_source_until_the_copy_is_complete(places, monkeypatch):
    sandbox, machine, ws, host, _ = places
    seen = []
    real = files._place

    def place(temporary, name, dir_fd, overwrite, identity=None):
        seen.append((name, (ws / "sub" / "b.txt").exists(), (ws / "sub" / "deeper" / "c.txt").exists()))
        return real(temporary, name, dir_fd, overwrite, identity)
    monkeypatch.setattr(files, "_place", place)
    result = files.move_entry(sandbox, "sub", machine, rel(host))
    assert result.moved and sorted(seen) == [("b.txt", True, True), ("c.txt", True, True)]
    assert not (ws / "sub").exists()


def test_a_failed_or_cancelled_move_keeps_the_source_and_leaves_nothing(places):
    sandbox, machine, ws, host, _ = places
    with pytest.raises(Refused, match="smaller part"):
        files.move_entry(sandbox, "sub", machine, rel(host), limit=6)  # b.txt fits, c.txt does not
    assert (ws / "sub" / "b.txt").read_text() == "beta" and (ws / "sub" / "deeper" / "c.txt").exists()
    assert list(host.iterdir()) == []
    calls = []
    with pytest.raises(Cancelled):
        files.move_entry(sandbox, "sub", machine, rel(host), cancelled=lambda: calls.append(1) or len(calls) > 3)
    assert (ws / "sub" / "b.txt").exists() and list(host.iterdir()) == []


def test_a_move_with_skipped_links_keeps_the_whole_source(places):
    sandbox, machine, ws, host, _ = places
    (ws / "sub" / "link").symlink_to("b.txt")
    result = files.move_entry(sandbox, "sub", machine, rel(host))
    assert not result.moved and "kept" in result.kept and (ws / "sub" / "b.txt").exists()
    assert (host / "sub" / "b.txt").exists() and [path for path, _ in result.skipped] == ["sub/link"]


def test_a_move_leaves_what_appeared_or_changed_after_it_was_copied(places, monkeypatch):
    sandbox, machine, ws, host, _ = places
    real = files._remove_copied

    def agent_meanwhile(root, dir_fd, name, shown, result, depth):
        if depth == 0:
            (ws / "sub" / "new.txt").write_text("written after the copy")
            (ws / "sub" / "b.txt").write_text("beta, changed")
        return real(root, dir_fd, name, shown, result, depth)
    monkeypatch.setattr(files, "_remove_copied", agent_meanwhile)
    result = files.move_entry(sandbox, "sub", machine, rel(host))
    assert not result.moved and sorted(result.left) == ["sub/b.txt", "sub/new.txt"]
    assert (ws / "sub" / "new.txt").exists() and (ws / "sub" / "b.txt").read_text() == "beta, changed"
    assert not (ws / "sub" / "deeper").exists(), "what was copied unchanged is gone from the source"
    assert (host / "sub" / "deeper" / "c.txt").exists()


def test_a_move_out_of_a_sandbox_within_one_root_is_a_copy_as_the_agents(places):
    """A cut in a host folder of a sandbox pasted into an ordinary host folder (one Root("/")): never a plain rename -
    the copy drops the executable bits and names what tools run, then the source goes."""
    _, machine, ws, host, _ = places
    (ws / "Makefile").write_text("all:\n")
    result = files.move_entry(machine, rel(ws / "run.sh"), machine, rel(host), untrusted=True, target_untrusted=False)
    assert result.moved and not (ws / "run.sh").exists() and (host / "run.sh").exists()
    assert not os.access(host / "run.sh", os.X_OK) and result.executable == ["run.sh"]
    result = files.move_entry(machine, rel(ws / "Makefile"), machine, rel(host), untrusted=True)
    assert result.moved and result.risky == ["Makefile"], "the target is not a sandbox's: copied (default)"
    # Within the sandbox's folders (or from the host into one) a move stays a rename.
    (ws / "into").mkdir()
    inode = (ws / "a.txt").stat().st_ino
    files.move_entry(machine, rel(ws / "a.txt"), machine, rel(ws / "into"), untrusted=True, target_untrusted=True)
    assert (ws / "into" / "a.txt").stat().st_ino == inode
    (host / "tool").write_text("#!/bin/sh\n")
    (host / "tool").chmod(0o755)
    inode = (host / "tool").stat().st_ino
    files.move_entry(machine, rel(host / "tool"), machine, rel(ws), untrusted=False)
    assert (ws / "tool").stat().st_ino == inode


# -- budgets, cancel, partial cleanup --

def test_copy_budgets_remove_what_the_copy_created(places):
    sandbox, machine, ws, host, _ = places
    with pytest.raises(Refused, match="smaller part"):
        files.copy_entry(sandbox, "sub", machine, rel(host), limit=6)
    with pytest.raises(Refused, match="smaller part"):
        files.copy_entry(sandbox, "sub", machine, rel(host), entries=3)
    assert list(host.iterdir()) == []


def test_a_cancelled_copy_leaves_nothing(places):
    sandbox, machine, _, host, _ = places
    calls = []
    with pytest.raises(Cancelled):
        files.copy_entry(sandbox, "sub", machine, rel(host), cancelled=lambda: calls.append(1) or len(calls) > 4)
    assert list(host.iterdir()) == []


def test_a_cancelled_copy_into_an_existing_folder_keeps_that_folder(places, tmp_path):
    sandbox, machine, ws, host, _ = places
    (host / "sub").mkdir()
    (host / "sub" / "mine.txt").write_text("mine")
    calls = []
    with pytest.raises(Cancelled):
        files.copy_entry(sandbox, "sub", machine, rel(host), overwrite=True,
                         cancelled=lambda: calls.append(1) or len(calls) > 6)
    assert (host / "sub" / "mine.txt").read_text() == "mine" and leftovers(host) == []


def test_delete_is_counted_first_and_cancellable(places):
    sandbox, _, ws, _, _ = places
    with pytest.raises(Refused, match="smaller part"):
        sandbox.delete_entry("sub", entries=3)
    assert (ws / "sub" / "deeper" / "c.txt").exists(), "a refused delete deletes nothing"
    with pytest.raises(Cancelled) as raised:  # b.txt is deleted first, then it is cancelled
        sandbox.delete_entry("sub", cancelled=lambda: not (ws / "sub" / "b.txt").exists())
    assert raised.value.partial.removed == 1 and (ws / "sub" / "deeper" / "c.txt").exists()
    progress = files.Progress()
    result = sandbox.delete_entry("sub", progress=progress)
    assert not (ws / "sub").exists() and progress.count == result.removed
    with pytest.raises(Refused):
        sandbox.delete_entry("")  # never the root itself


def test_progress_counts_entries_and_bytes(places):
    sandbox, machine, _, host, _ = places
    progress = files.Progress()
    files.copy_entry(sandbox, "sub", machine, rel(host), progress=progress)
    assert progress.count == 4 and progress.bytes == len("beta") + len("gamma")


# -- rename, new folder --

def test_rename_and_new_folder(places):
    sandbox, _, ws, _, _ = places
    sandbox.rename_entry("a.txt", "b.txt")
    assert (ws / "b.txt").read_text() == "alpha"
    with pytest.raises(Refused, match="already exists"):
        sandbox.rename_entry("b.txt", "sub")
    for bad in ("", ".", "..", "x/y", "x\0y", "n" * 256):
        with pytest.raises(Refused):
            sandbox.rename_entry("b.txt", bad)
        with pytest.raises(Refused):
            sandbox.make_folder("", bad)
    sandbox.make_folder("sub", "new")
    assert (ws / "sub" / "new").is_dir()
    for existing in ("new", "../new"):
        with pytest.raises(Refused):
            sandbox.make_folder("sub", existing)
    with pytest.raises(Refused, match="already exists"):
        sandbox.make_folder("", "planted")  # a link there is not replaced (nor followed)


def test_a_rename_of_only_the_letter_case_without_renameat2(places, monkeypatch):
    """Review: without renameat2 (macOS) the new name was checked first - on a file system that ignores the case it
    "exists" (the file itself), and a.txt -> A.txt was refused. Shown here with a hard link of another case (the same
    file under both names, as such a file system sees it); any other existing name is still refused."""
    import ctypes
    sandbox, _, ws, _, _ = places
    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: (_ for _ in ()).throw(OSError("no libc here")))
    os.link(ws / "a.txt", ws / "A.txt")
    sandbox.rename_entry("a.txt", "A.txt")
    assert (ws / "A.txt").read_text() == "alpha"
    (ws / "other.txt").write_text("other")
    with pytest.raises(Refused, match="already exists"):
        sandbox.rename_entry("A.txt", "other.txt")
    assert (ws / "other.txt").read_text() == "other"


# -- chmod, chown --

def test_chmod(places):
    sandbox, machine, ws, host, _ = places
    sandbox.chmod_entry("a.txt", 0o640)
    assert stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o640
    sandbox.chmod_entry("a.txt", 0)
    sandbox.chmod_entry("a.txt", 0o644)  # also a file nobody may read
    assert stat.S_IMODE((ws / "a.txt").stat().st_mode) == 0o644
    sandbox.chmod_entry("sub", 0o700)
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o700
    for bad in (-1, 0o10000):
        with pytest.raises(Refused):
            sandbox.chmod_entry("a.txt", bad)


@pytest.mark.parametrize("bit", [stat.S_ISUID, stat.S_ISGID, stat.S_ISVTX])
def test_no_setuid_setgid_or_sticky_bit_below_a_sandbox_root(places, bit):
    sandbox, machine, ws, host, _ = places
    for path in ("run.sh", "sub"):
        with pytest.raises(Refused, match="never below a sandbox"):
            sandbox.chmod_entry(path, 0o755 | bit)
        with pytest.raises(Refused):
            sandbox.chmod_entry(path, 0o755 | bit, special=True)
    assert not (ws / "run.sh").stat().st_mode & files.SPECIAL_BITS
    (host / "tool").write_text("x")
    with pytest.raises(Refused):
        machine.chmod_entry(rel(host / "tool"), 0o755 | bit)  # on the host only when asked for
    machine.chmod_entry(rel(host / "tool"), 0o755 | bit, special=True)
    assert (host / "tool").stat().st_mode & bit


def test_a_set_group_id_bit_a_sandbox_folder_has_may_stay_or_go(places):
    """Review: a folder below a set-group-ID one inherits the bit; the Properties dialog keeps it (its box greyed
    out) and every change of such a folder in a sandbox was refused."""
    sandbox, _, ws, _, _ = places
    (ws / "sub").chmod(0o2775)
    if not (ws / "sub").stat().st_mode & stat.S_ISGID:
        pytest.skip("the set-group-ID bit is not kept here (a group of another)")
    sandbox.chmod_entry("sub", 0o2750)
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o2750
    with pytest.raises(Refused, match="never below a sandbox"):
        sandbox.chmod_entry("sub", 0o2750 | stat.S_ISVTX)  # a bit it has not: never added
    sandbox.chmod_entry("sub", 0o750)
    assert stat.S_IMODE((ws / "sub").stat().st_mode) == 0o750


def test_chown_only_to_oneself_and_ones_own_groups(places):
    sandbox, _, ws, _, _ = places
    result = sandbox.chown_entry("sub", os.getuid(), os.getgid(), recursive=True)
    assert result.files == 4 and result.skipped == []
    result = sandbox.chown_entry("sub", -1, files.user_groups()[0])
    assert result.files == 1
    with pytest.raises(Refused):
        sandbox.chown_entry("a.txt", os.getuid() + 1, -1)
    foreign = max(files.user_groups()) + 4242
    with pytest.raises(Refused):
        sandbox.chown_entry("a.txt", -1, foreign)
    with pytest.raises(Refused, match="smaller part"):
        sandbox.chown_entry("sub", os.getuid(), -1, recursive=True, entries=2)


# -- taking files back from a rootless Podman sandbox --

@pytest.fixture
def fake_podman(tmp_path):
    """`podman unshare CMD...` that runs CMD as it is (no user namespace): the helper then works as the user."""
    script = tmp_path / "podman"
    script.write_text('#!/bin/sh\n[ "$1" = unshare ] || exit 9\nshift\nexec "$@"\n')
    script.chmod(0o755)
    return str(script)


def test_subordinate_ranges(tmp_path):
    subuid = tmp_path / "subuid"
    subuid.write_text(f"someone:100000:65536\n{os.getuid()}:200000:65536\nbroken\nx:y:z\n")
    assert files.subordinate_ranges(str(subuid)) == [(200000, 65536)]
    assert files.subordinate_ranges(str(tmp_path / "missing")) == []
    assert files.ownership_problem(os.getuid(), []) is None
    assert files.ownership_problem(200001, [(200000, 65536)]) is None
    assert "UID 99" in files.ownership_problem(99, [(200000, 65536)])
    assert files.ownership_problem(265536, [(200000, 65536)]) is not None


def test_the_command_is_podman_unshare_chown_to_namespace_root(places):
    sandbox, *_ = places
    info = sandbox.lstat("sub")
    argv = files.ownership_command("/usr/bin/podman", sandbox.path, "sub", info)
    assert argv[:2] == ["/usr/bin/podman", "unshare"] and argv[2:4] == [sys.executable, "-I"]
    assert argv[-6:] == [sandbox.path, "sub", str(info.st_dev), str(info.st_ino), "0", "0"]


def test_take_ownership_refuses_the_host_links_foreign_owners_and_no_podman(places, tmp_path, fake_podman, monkeypatch):
    sandbox, machine, ws, host, _ = places
    (host / "x").write_text("x")
    with pytest.raises(Refused, match="only below a sandbox"):
        files.take_ownership(machine, rel(host / "x"), podman=fake_podman)
    with pytest.raises(Refused):
        files.take_ownership(sandbox, "", podman=fake_podman)
    with pytest.raises(Refused, match="symbolic link"):
        files.take_ownership(sandbox, "planted", podman=fake_podman)
    monkeypatch.setattr(files.shutil, "which", lambda name: None)  # a runner may have Podman (GitHub's has)
    with pytest.raises(Refused, match="Podman was not found"):
        files.take_ownership(sandbox, "sub", podman="")
    monkeypatch.undo()
    if can_give_away(4242):
        os.chown(ws / "a.txt", 4242, 4242)
        with pytest.raises(Refused, match="UID 4242"):
            files.take_ownership(sandbox, "a.txt", podman=fake_podman, subuid=str(tmp_path / "none"))


def test_take_ownership_runs_the_helper(places, tmp_path, fake_podman, monkeypatch):
    sandbox, *_ = places
    monkeypatch.setattr(files.shutil, "which", lambda name: None)
    with pytest.raises(Refused, match="Podman was not found"):
        files.take_ownership(sandbox, "sub")
    me = (os.getuid(), os.getgid())
    result = files.take_ownership(sandbox, "sub", podman=fake_podman, owner=me)
    assert result.files == 0 and result.skipped == []  # all the user's already


def test_the_helper_walks_every_entry_without_following_links(places, fake_podman):
    """Without a user namespace the helper cannot hand files to a foreign group: every entry it reaches is
    reported, the link's target outside is never among them."""
    sandbox, _, ws, _, outside = places
    (ws / "sub" / "to-outside").symlink_to(outside)
    foreign = max(files.user_groups()) + 54321
    result = files.take_ownership(sandbox, "sub", podman=fake_podman, owner=(os.getuid(), foreign))
    assert result.files == 0
    assert sorted(path for path, _ in result.skipped) == ["sub", "sub/b.txt", "sub/deeper", "sub/deeper/c.txt",
                                                          "sub/to-outside"]


@pytest.mark.skipif(not SUBORDINATE, reason="files of a subordinate UID can only be made as root")
def test_take_ownership_takes_subordinate_uid_files_back_and_follows_no_link(places, tmp_path, fake_podman):
    sandbox, _, ws, _, outside = places
    subuid = tmp_path / "subuid"
    subuid.write_text(f"{os.getuid()}:100000:65536\n")
    (ws / "sub" / "to-precious").symlink_to(outside / "precious.txt")
    os.chown(outside / "precious.txt", 100007, 100007)
    for path in [ws / "sub", *(ws / "sub").rglob("*")]:
        os.chown(path, 100999, 100999, follow_symlinks=False)
    me = (os.getuid(), os.getgid())
    result = files.take_ownership(sandbox, "sub", podman=fake_podman, subuid=str(subuid), owner=me)
    assert result.files == 6 and result.skipped == []
    assert all((p.lstat().st_uid, p.lstat().st_gid) == me for p in [ws / "sub", *(ws / "sub").rglob("*")])
    assert (outside / "precious.txt").stat().st_uid == 100007, "a link is changed itself, never its target"


def test_take_ownership_refuses_an_entry_replaced_after_the_check(places, tmp_path, fake_podman, monkeypatch):
    sandbox, _, ws, _, outside = places
    real = files.ownership_command

    def swapped(podman, root_path, relative, info, owner=(0, 0)):
        (ws / "sub").rename(ws / "old-sub")
        (ws / "sub").symlink_to(outside)
        return real(podman, root_path, relative, info, owner)
    monkeypatch.setattr(files, "ownership_command", swapped)
    with pytest.raises(Refused, match="failed"):
        files.take_ownership(sandbox, "sub", podman=fake_podman, owner=(os.getuid(), os.getgid()))


def test_take_ownership_can_be_cancelled(places, tmp_path):
    sandbox, *_ = places
    slow = tmp_path / "slow-podman"
    slow.write_text("#!/bin/sh\nexec sleep 30\n")
    slow.chmod(0o755)
    with pytest.raises(Cancelled):
        files.take_ownership(sandbox, "sub", podman=str(slow), cancelled=lambda: True)


# -- what the panes ask before they copy --

def test_exists_in_names_what_is_there_also_links(places):
    sandbox, *_ = places
    assert files.exists_in(sandbox, "", ["a.txt", "planted", "nothing", "sub"]) == ["a.txt", "planted", "sub"]
    with pytest.raises(Refused):
        files.exists_in(sandbox, "escape", ["precious.txt"])


def test_listing_has_owner_and_mode(places):
    sandbox, *_ = places
    entries = {entry.name: entry for entry in sandbox.list()}
    assert entries["run.sh"].mode & 0o111 and entries["run.sh"].uid == os.getuid()
    assert stat.S_ISLNK(entries["planted"].mode) and entries["sub"].gid == os.getgid()
