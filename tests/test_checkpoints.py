"""Workspace checkpoints (sandbox/checkpoints.py): a round trip of everything a workspace holds (files, folders,
symbolic links - never followed -, permissions, deletions, renames, the project's .git), the preview of a restore,
single paths, a restore that never writes outside its folder, the exclude list, special files, the store's dedup and
limits, and the command line."""
import gzip
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import home_guard  # noqa: E402

home_guard.ensure()


def load():
    spec = importlib.util.spec_from_file_location("sandbox_checkpoints_test", ROOT / "sandbox/checkpoints.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


C = load()


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "home/.local/state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "home/.config"))
    (tmp_path / "home").mkdir()
    return tmp_path


def tree(root):
    """{relative path: (kind, mode, content or link target)} of ROOT, links not followed."""
    found = {}
    root = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                found[rel] = ("link", None, os.readlink(path))
            elif stat.S_ISDIR(info.st_mode):
                found[rel] = ("dir", stat.S_IMODE(info.st_mode), None)
            elif stat.S_ISREG(info.st_mode):
                found[rel] = ("file", stat.S_IMODE(info.st_mode), path.read_bytes())
            else:
                found[rel] = ("special", None, None)
    return found


def workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "src/pkg").mkdir(parents=True)
    (ws / "src/pkg/a.py").write_text("print('a')\n")
    (ws / "src/b.txt").write_text("b\n")
    script = ws / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    secret = ws / "private.key"
    secret.write_text("k")
    secret.chmod(0o600)
    (ws / "empty").mkdir()
    (ws / "link-to-a").symlink_to("src/pkg/a.py")
    (ws / ".git/objects/ab").mkdir(parents=True)
    (ws / ".git/HEAD").write_text("ref: refs/heads/main\n")
    (ws / ".git/objects/ab/cdef").write_bytes(b"\x78\x9cblob")
    (ws / "big.bin").write_bytes(os.urandom(5 << 20))  # larger than LARGE: read in the pool, streamed (or cloned)
    return ws


def roots(ws):
    return [{"host": str(ws), "sandbox": "/work", "kind": "workspace"}]


def test_a_round_trip_brings_back_files_folders_links_modes_and_git(tmp_path):
    ws = workspace(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("never in a checkpoint")
    (ws / "to-outside").symlink_to(outside)
    before = tree(ws)
    meta = C.take(str(ws), roots(ws), kind="start", run="claude-1", name="web")
    assert meta["files"] == 7 and meta["kind"] == "start"
    # What the agent does: deletes, renames, rewrites, changes modes, wrecks .git, replaces a folder by a link.
    (ws / "src/b.txt").unlink()
    (ws / "src/pkg/a.py").rename(ws / "src/pkg/renamed.py")
    (ws / "run.sh").write_text("rm -rf /\n")
    (ws / "private.key").chmod(0o644)
    import shutil
    shutil.rmtree(ws / ".git")
    shutil.rmtree(ws / "empty")
    (ws / "empty").symlink_to(tmp_path)
    (ws / "new.txt").write_text("new")
    (ws / "link-to-a").unlink()
    (ws / "link-to-a").symlink_to("/etc/passwd")
    result = C.restore(meta["id"])
    assert tree(ws) == before
    assert outside.read_text() == "never in a checkpoint"
    assert result["counts"]["added"] >= 2 and result["counts"]["deleted"] >= 3
    # The links were stored as links: their targets' contents never entered the store.
    store = C.Store.of(str(ws))
    contents = {p.read_bytes() for p in store.objects.rglob("*") if p.is_file()}
    assert b"never in a checkpoint" not in contents
    # The restore took a checkpoint of the state before it: undone, the agent's state is back.
    C.restore(result["before"])
    assert (ws / "new.txt").read_text() == "new" and not (ws / ".git").exists()
    assert (ws / "empty").is_symlink() and os.readlink(ws / "link-to-a") == "/etc/passwd"


def test_the_preview_says_what_changes_and_a_dry_run_changes_nothing(tmp_path, capsys):
    ws = workspace(tmp_path)
    meta = C.take(str(ws), roots(ws), kind="start")
    (ws / "src/b.txt").write_text("changed\n")
    (ws / "run.sh").chmod(0o700)
    (ws / "added").mkdir()
    (ws / "added/x").write_text("x")
    (ws / "private.key").unlink()
    found = C.diff(meta["id"])
    [root] = found["roots"]
    assert root["added"] == ["added", "added/x"] and root["deleted"] == ["private.key"]
    assert root["changed"] == [["run.sh", "mode"], ["src/b.txt", "content"]]
    assert found["counts"] == {"added": 2, "deleted": 1, "changed": 2}
    now = tree(ws)
    assert C.main(["restore", meta["id"], "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "M /work/src/b.txt" in text and "A /work/added/x" in text and "D /work/private.key" in text
    assert "2 changed, 2 added, 1 deleted" in text
    assert tree(ws) == now
    # A file touched (same content, new time) is no change of content.
    os.utime(ws / "src/pkg/a.py", (1, 1))
    assert "src/pkg/a.py" not in json.dumps(C.diff(meta["id"]))


def test_one_path_alone_is_restored(tmp_path):
    ws = workspace(tmp_path)
    meta = C.take(str(ws), roots(ws))
    (ws / "src/b.txt").write_text("agent\n")
    (ws / "src/pkg/a.py").write_text("agent\n")
    (ws / "run.sh").write_text("agent\n")
    result = C.restore(meta["id"], paths=["/work/src/pkg"])
    assert (ws / "src/pkg/a.py").read_text() == "print('a')\n"
    assert (ws / "src/b.txt").read_text() == "agent\n" and (ws / "run.sh").read_text() == "agent\n"
    assert result["counts"]["changed"] == 1
    C.restore(meta["id"], paths=["src/b.txt", str(ws / "run.sh")])  # relative to /work, or the host path
    assert (ws / "src/b.txt").read_text() == "b\n" and (ws / "run.sh").read_text().startswith("#!/bin/sh")
    with pytest.raises(ValueError, match="none of the checkpoint's folders"):
        C.restore(meta["id"], paths=["/etc/passwd"])
    with pytest.raises(ValueError, match="outside"):
        C.diff(meta["id"], paths=["../escape"])


def test_a_path_restored_into_folders_that_are_gone_puts_them_back_as_they_were(tmp_path):
    """Review 3x-2: restoring src/pkg after the agent removed src made src anew - 0700 and the time of now, not as
    the checkpoint holds it; the folders on the way to a path get their modes and times back too."""
    ws = workspace(tmp_path)
    (ws / "src").chmod(0o750)
    os.utime(ws / "src", (1_600_000_000, 1_600_000_000))
    meta = C.take(str(ws), roots(ws))
    import shutil
    shutil.rmtree(ws / "src")
    C.restore(meta["id"], paths=["/work/src/pkg"])
    assert (ws / "src/pkg/a.py").read_text() == "print('a')\n" and not (ws / "src/b.txt").exists()
    info = (ws / "src").stat()
    assert stat.S_IMODE(info.st_mode) == 0o750 and int(info.st_mtime) == 1_600_000_000


def test_a_restore_never_writes_outside_its_folder(tmp_path):
    ws = workspace(tmp_path)
    meta = C.take(str(ws), roots(ws))
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "pkg").mkdir()
    # The agent replaces src by a link to a folder of yours, hoping the restore writes src/pkg/a.py there.
    import shutil
    shutil.rmtree(ws / "src")
    (ws / "src").symlink_to(victim)
    C.restore(meta["id"])
    assert sorted(p.name for p in victim.rglob("*")) == ["pkg"], "nothing written through the link"
    assert (ws / "src").is_dir() and not (ws / "src").is_symlink()
    assert (ws / "src/pkg/a.py").read_text() == "print('a')\n"
    # A manifest naming a path outside its folder is refused as damaged, before anything is written.
    store = C.Store.of(str(ws))
    path = store.manifest_path(meta["id"])
    document = json.loads(gzip.decompress(path.read_bytes()))
    document["roots"][0]["entries"].append(["f", "../escaped", 0o644, 0, 1, "0" * 64, 1, 1])
    path.write_bytes(gzip.compress(json.dumps(document).encode()))
    with pytest.raises(ValueError, match="outside its folder"):
        C.restore(meta["id"])
    assert not (tmp_path / "escaped").exists()


def test_a_damaged_object_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("original")
    meta = C.take(str(ws), roots(ws))
    store = C.Store.of(str(ws))
    [obj] = [p for p in store.objects.rglob("*") if p.is_file()]
    obj.chmod(0o600)
    obj.write_text("tampered")
    (ws / "a").write_text("agent")
    with pytest.raises(ValueError, match="damaged"):
        C.restore(meta["id"])
    assert (ws / "a").read_text() == "agent", "the damaged copy was not written"


def test_excluded_folders_and_special_files(tmp_path):
    ws = tmp_path / "ws"
    (ws / "node_modules/x").mkdir(parents=True)
    (ws / "node_modules/x/index.js").write_text("big")
    (ws / "sub/.venv").mkdir(parents=True)
    (ws / "keep.txt").write_text("k")
    os.mkfifo(ws / "pipe")
    meta = C.take(str(ws), roots(ws))
    [root] = meta["roots"]
    assert root["excluded"] == 2 and root["files"] == 1
    assert ["pipe", "a FIFO: not checkpointed"] in root["skipped"]
    (ws / "node_modules/x/index.js").write_text("rebuilt")
    (ws / "keep.txt").unlink()
    C.restore(meta["id"])
    assert (ws / "node_modules/x/index.js").read_text() == "rebuilt", "excluded: not touched"
    assert (ws / "keep.txt").read_text() == "k" and stat.S_ISFIFO((ws / "pipe").lstat().st_mode)
    # Your own exclude list (defaults.json): checked.
    (tmp_path / "home/.config/sandbox-ai").mkdir(parents=True)
    (tmp_path / "home/.config/sandbox-ai/defaults.json").write_text(json.dumps(
        {"version": 1, "checkpoint_exclude": ["*.log", "build/cache"]}))
    assert C.settings()["checkpoint_exclude"] == ["*.log", "build/cache"]
    with pytest.raises(ValueError, match="list of names"):
        C.check_setting("checkpoint_exclude", ["../x"], "defaults")
    with pytest.raises(ValueError, match="1 to 1440"):
        C.check_setting("checkpoint_every", 5000, "defaults")


def test_unchanged_files_are_not_read_again_and_content_is_kept_once(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    for i in range(20):
        (ws / f"f{i}").write_text("same content")
    first = C.take(str(ws), roots(ws))
    store = C.Store.of(str(ws))
    assert len([p for p in store.objects.rglob("*") if p.is_file() and p.parent.name != "tmp"]) == 1
    assert first["new_bytes"] == len("same content")
    reads = []
    real = C.Store.put
    monkeypatch.setattr(C.Store, "put", lambda self, fd, size: reads.append(size) or real(self, fd, size))
    time.sleep(0.01)
    (ws / "f3").write_text("other")
    second = C.take(str(ws), roots(ws))
    assert reads == [5], "only the changed file was read"
    assert second["new_bytes"] == 5


def test_a_file_written_while_it_is_read_is_kept_as_read(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "log").write_text("one\n")
    real = C.Store.put

    def put(self, fd, size):
        done = real(self, fd, size)
        with open(ws / "log", "a") as stream:
            stream.write("two\n")
        return done
    monkeypatch.setattr(C.Store, "put", put)
    meta = C.take(str(ws), roots(ws))
    assert meta["roots"][0]["torn"] == 1
    monkeypatch.setattr(C.Store, "put", real)
    reads = []
    monkeypatch.setattr(C.Store, "put", lambda self, fd, size: reads.append(size) or real(self, fd, size))
    C.take(str(ws), roots(ws))
    assert reads, "read again at the next checkpoint"


def test_a_workspace_that_is_a_file(tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("mine")
    meta = C.take(str(notes), [{"host": str(notes), "sandbox": "/work", "kind": "workspace"}])
    notes.write_text("agent")
    assert C.diff(meta["id"])["counts"]["changed"] == 1
    C.restore(meta["id"])
    assert notes.read_text() == "mine"
    notes.unlink()
    C.restore(meta["id"])
    assert notes.read_text() == "mine"


def test_the_limits_keep_the_newest_and_drop_periodic_ones_first(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    made = []
    for i, kind in enumerate(("start", "periodic", "periodic", "manual", "periodic")):
        (ws / "f").write_text(f"version {i}" + "x" * 1000)
        made.append(C.take(str(ws), roots(ws), kind=kind, prune_after=False)["id"])
    store = C.Store.of(str(ws))
    limits = {**C.SETTINGS, "checkpoint_count": 3, "checkpoint_days": 0, "checkpoint_gb": 0}
    done = C.prune(store, limits)
    assert sorted(done["removed"]) == sorted(made[1:3]), "the oldest periodic ones went first"
    assert [m["id"] for m in store.metas()] == [made[0], made[3], made[4]]
    objects = [p for p in store.objects.rglob("*") if p.is_file() and p.parent.name != "tmp"]
    assert len(objects) == 3, "what only the dropped ones held is gone"
    # By age: all but the newest.
    old = {**limits, "checkpoint_count": 0, "checkpoint_days": 1}
    assert len(C.prune(store, old, now=time.time() + 3 * 86400)["removed"]) == 2
    assert [m["id"] for m in store.metas()] == [made[4]]
    # By size: beyond it, the oldest go (never the newest).
    for i in range(3):
        (ws / f"g{i}").write_bytes(os.urandom(4096))
        C.take(str(ws), roots(ws), prune_after=False)
    tiny = {**limits, "checkpoint_count": 0, "checkpoint_gb": 0}
    assert C.prune(store, tiny)["removed"] == []
    store.set_usage(10 << 30)  # (recorded size beyond 1 GiB: the exact pass runs)
    found = C.prune(store, {**tiny, "checkpoint_gb": 1})
    assert found["removed"] == [] and found["bytes"] < 1 << 20


def test_gc_prunes_every_store_and_delete_removes_content(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    one = C.take(str(ws), roots(ws), prune_after=False)
    (ws / "a").write_text("b")
    two = C.take(str(ws), roots(ws), prune_after=False)
    [found] = C.prune_all(dry_run=True)
    assert found["removed"] == [] and found["project"] == str(ws)
    assert C.delete([one["id"]]) == [one["id"]]
    assert [m["id"] for m in C.listing()] == [two["id"]]
    with pytest.raises(ValueError, match="no checkpoint"):
        C.find(one["id"])
    with pytest.raises(ValueError, match="no checkpoint id"):
        C.find("../../etc")


def test_the_background_checkpoint_and_the_periodic_one(tmp_path):
    ws = workspace(tmp_path)
    pending = C.Pending(str(ws), roots(ws), kind="start", run="claude-x", name="web")
    meta = pending.finish(start="web")
    assert meta["kind"] == "start" and meta["start"] == "web"
    assert C.read_meta(C.Store.of(str(ws)).meta_path(meta["id"]))["start"] == "web"
    periodic = C.Periodic(60, str(ws), roots(ws), run="claude-x", name="web")
    try:
        assert periodic.once() is None, "nothing changed: none taken"
        (ws / "src/b.txt").write_text("changed")
        taken = periodic.once()
        assert taken["kind"] == "periodic" and taken["run"] == "claude-x"
        # A change of the same size (and a new time) is seen too, without reading every file.
        time.sleep(0.01)
        (ws / "src/b.txt").write_text("CHANGED")
        again = periodic.once()
        assert again is not None and again["id"] != taken["id"]
    finally:
        periodic.stop()
    failing = C.Pending(str(tmp_path / "gone"), [{"host": "relative/path"}])
    with pytest.raises(ValueError, match="absolute"):
        failing.finish()


def test_the_command_line(tmp_path, capsys, monkeypatch):
    ws = workspace(tmp_path)
    meta = C.take(str(ws), roots(ws), kind="start", run="claude-1", start="web", name="web")
    C.record_run("web", {"run": "claude-1", "start": "web", "name": "web", "agent": "claude", "project": str(ws),
                         "roots": roots(ws), "checkpoint": meta["id"]})
    assert C.main(["web", "--label", "before the refactoring"]) == 0
    assert "of web:" in capsys.readouterr().out
    assert C.main(["list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [row["kind"] for row in rows] == ["manual", "start"] and rows[0]["label"] == "before the refactoring"
    assert C.main(["list", "web", "--json"]) == 0 and len(json.loads(capsys.readouterr().out)) == 2
    assert C.main(["show", meta["id"], "--files", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert {"path": "link-to-a", "type": "link", "target": "src/pkg/a.py"} in shown["paths"][0]["entries"]
    import shutil
    shutil.rmtree(ws / "src")
    assert C.main(["diff", meta["id"], "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["deleted"] == 4
    # restore RUN: its start checkpoint; nothing can ask here, so --yes is needed.
    monkeypatch.setattr(sys, "stdin", open(os.devnull))
    assert C.main(["restore", "web"]) == 1
    assert "--yes" in capsys.readouterr().err and not (ws / "src").exists()
    assert C.main(["restore", "web", "--yes", "--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["restored"] is True and done["restore"] == meta["id"] and (ws / "src/pkg/a.py").exists()
    assert C.main(["delete", done["before"], "--yes"]) == 0
    assert C.main(["show", "20990101-000000-abcdef"]) == 1
    assert "no checkpoint" in capsys.readouterr().err
    assert C.main(["restore", "nobody", "--yes"]) == 1
    assert "no start named nobody" in capsys.readouterr().err


def test_the_store_lies_in_sandbox_ais_own_state(tmp_path):
    """The agent cannot reach the store: it lies in sandbox-ai's state folder, which no start mounts (a workspace
    that holds it - your HOME - never has it in a checkpoint either)."""
    assert C.store_root() == tmp_path / "home/.local/state/sandbox-ai/checkpoints"
    home = tmp_path / "home"
    (home / "project.txt").write_text("p")
    first = C.take(str(home), [{"host": str(home), "sandbox": "/work"}])
    second = C.take(str(home), [{"host": str(home), "sandbox": "/work"}])
    assert any(why.startswith("sandbox-ai's own state") for _, why in second["roots"][0]["skipped"])
    assert second["files"] == first["files"] == 1


def test_a_start_checkpoint_is_taken_also_when_its_thread_never_runs(tmp_path, monkeypatch):
    """Found by the launcher's matrix tests, which stand in for threading.Thread with threads that run nothing: the
    start's checkpoint then came back as None (and the start failed). finish() takes it itself then."""
    import threading
    real = threading.Thread
    monkeypatch.setattr(C.threading, "Thread", lambda *a, target=None, **kw: real(target=lambda *x, **y: None))
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    meta = C.Pending(str(ws), roots(ws), kind="start").finish()
    assert meta["kind"] == "start" and meta["files"] == 1


def test_an_interrupted_checkpoint_leaves_nothing_behind_after_gc(tmp_path, monkeypatch):
    """Review 3x-2: a checkpoint ended mid-way (Ctrl-C at a start, a start that failed while its checkpoint ran, the
    machine off) wrote content no checkpoint holds: the next prune (after a checkpoint, sandbox-ai gc) removes it,
    and what objects/tmp holds - also when nothing is old enough to go."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    C.take(str(ws), roots(ws))
    for index in range(5):
        (ws / f"new{index}").write_text(f"new {index}")
    written = C.write_json

    def interrupted(path, document, compress=False):
        if "manifests" in str(path):
            raise KeyboardInterrupt
        return written(path, document, compress)
    monkeypatch.setattr(C, "write_json", interrupted)
    with pytest.raises(KeyboardInterrupt):
        C.take(str(ws), roots(ws))
    monkeypatch.setattr(C, "write_json", written)
    store = C.Store.of(str(ws))
    (store.objects / "tmp" / "0123456789abcdef").write_bytes(b"half a file")

    def objects():
        return sorted(p.name for p in store.objects.glob("??/*"))
    assert len(objects()) == 6
    [found] = C.prune_all()
    assert found["removed"] == []
    assert len(objects()) == 1 and not list((store.objects / "tmp").iterdir())
    assert store.usage() == 1
    # A checkpoint that completes clears what it marked: the prune after it does not walk every manifest again.
    C.take(str(ws), roots(ws), prune_after=False)
    called = []
    monkeypatch.setattr(C, "object_references", lambda *args: called.append(args) or ({}, {}, {}))
    C.prune(store)
    assert called == []


def test_a_checkpoint_never_fills_the_disk_and_what_it_wrote_goes_at_once(tmp_path, monkeypatch):
    """Review 3x-2: a checkpoint is taken at every start, of whatever the workspace holds - a first one copies every
    file. It stops before the store's disk runs full (a reserve stays free: the run, the machine go on), says why and
    what helps, and what it wrote before is removed at once (the next start would not find the disk full again)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    first = C.take(str(ws), roots(ws))
    store = C.Store.of(str(ws))
    for index in range(40):
        (ws / f"data{index}.bin").write_bytes(os.urandom(64 << 10) + bytes([index]))
    (ws / "big.bin").write_bytes(os.urandom(5 << 20))
    total = 100 << 30
    free = {"bytes": (1 << 30) + (1 << 20)}  # a little more than the reserve (1 % of 100 GiB) when it begins
    put = C.Store.put

    def counted(self, fd, size_hint):
        found = put(self, fd, size_hint)
        free["bytes"] -= max(found[1], 64 << 10)
        return found
    monkeypatch.setattr(C.Store, "put", counted)
    monkeypatch.setattr(C, "SPACE_STEP", 1)  # (looked at before every file here)
    monkeypatch.setattr(C, "disk_space", lambda path: (free["bytes"], total))
    with pytest.raises(ValueError, match=r"free.*checkpoint_exclude") as said:
        C.take(str(ws), roots(ws))
    assert isinstance(said.value, C.NoSpace)
    assert [m["id"] for m in C.listing()] == [first["id"]]
    assert len(list(store.objects.glob("??/*"))) == 1 and not list((store.objects / "tmp").iterdir())
    assert not store.taking().exists() and store.usage() == 1
    # With room again it is taken as always.
    free["bytes"] = 50 << 30
    assert C.take(str(ws), roots(ws))["files"] == 42


def test_a_failed_checkpoint_removes_what_it_wrote(tmp_path, monkeypatch):
    """A write that fails (the disk full after all, an I/O error) ends the checkpoint the same way: nothing of it
    stays in the store."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    C.take(str(ws), roots(ws))
    store = C.Store.of(str(ws))
    for index in range(10):
        (ws / f"n{index}").write_text(f"n{index}")
    written, calls = C.write_all, []

    def full(fd, data):
        calls.append(1)
        if len(calls) > 5:
            raise OSError(28, "No space left on device")
        return written(fd, data)
    monkeypatch.setattr(C, "write_all", full)
    with pytest.raises(OSError, match="No space"):
        C.take(str(ws), roots(ws))
    assert len(list(store.objects.glob("??/*"))) == 1 and not list((store.objects / "tmp").iterdir())
    assert not store.taking().exists()


def test_gc_forgets_the_run_records_no_checkpoint_needs_any_more(tmp_path):
    """Review 3x-2: every start records which folders its checkpoints hold (runs/KEY.json, for `checkpoint RUN`) -
    every scheduled run too -, and nothing removed them: a folder of thousands, all read for each `restore NAME`.
    sandbox-ai gc forgets those a month old whose checkpoints are all gone (never one of a running start)."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a").write_text("a")
    kept = C.take(str(ws), roots(ws), kind="start", run="run-kept", start="kept")
    for key in ("kept", "gone", "young", "live"):
        C.record_run(key, {"run": f"run-{key}", "start": key, "name": key, "project": str(ws), "roots": roots(ws)})
    month = time.time() - 40 * 86400
    for key in ("kept", "gone", "live"):
        os.utime(C.runs_dir() / f"{key}.json", (month, month))
    assert C.prune_all(dry_run=True) and len(list(C.runs_dir().iterdir())) == 4
    C.prune_all(live=["run-live"])
    assert sorted(p.name for p in C.runs_dir().iterdir()) == ["kept.json", "live.json", "young.json"]
    assert C.resolve_run("kept")[0]["key"] == "kept" and kept["start"] == "kept"


def test_the_room_a_checkpoint_needs_counts_the_files_still_being_read(tmp_path, monkeypatch):
    """Large files are read by a pool (up to IN_FLIGHT at once): the free space looked at does not show them yet, so
    what the pool still reads counts against it too."""
    import threading as threads
    ws = tmp_path / "ws"
    ws.mkdir()
    for index in range(4):
        (ws / f"big{index}.bin").write_bytes(os.urandom(5 << 20))
    gate = threads.Event()
    put = C.Store.put

    def slow(self, fd, size_hint):
        gate.wait(5)  # (nothing is written while the walk goes on)
        return put(self, fd, size_hint)
    monkeypatch.setattr(C.Store, "put", slow)
    monkeypatch.setattr(C, "SPACE_STEP", 1)
    total = 10 << 30
    monkeypatch.setattr(C, "disk_space", lambda path: ((1 << 30) + (12 << 20), total))  # the reserve and 12 MiB
    timer = threads.Timer(1.0, gate.set)
    timer.start()
    try:
        with pytest.raises(C.NoSpace):
            C.take(str(ws), roots(ws))
    finally:
        gate.set()
        timer.cancel()


def test_a_folder_larger_than_the_room_left_is_refused_before_anything_is_copied(tmp_path, monkeypatch):
    """Review 3x-2: a folder's first checkpoint copies every file. When its files hold more than the store's disk
    can take, it is said at once - with the folder's size - instead of after copying most of it (at every start
    again); a file system that clones (reflinks) needs room only for the small files."""
    ws = tmp_path / "ws"
    ws.mkdir()
    for index in range(3):
        (ws / f"big{index}.bin").write_bytes(os.urandom(3 << 20))
    (ws / "small.txt").write_text("small")
    store = C.Store.of(str(ws))
    put = C.Store.put
    copied = []
    monkeypatch.setattr(C.Store, "put", lambda self, fd, hint: copied.append(hint) or put(self, fd, hint))
    monkeypatch.setattr(C, "disk_space", lambda path: ((1 << 30) + (5 << 20), 10 << 30))  # the reserve and 5 MiB
    monkeypatch.setattr(C, "clone", lambda src, out, path: (_ for _ in ()).throw(OSError(95, "not supported")))
    with pytest.raises(C.NoSpace, match=r"/work holds 9\.0 MiB.*only 1\.0 GiB free"):
        C.take(str(ws), roots(ws))
    assert copied == [] and not list(store.objects.glob("??/*")) and C.listing() == []
    # Where the store's file system clones, the large files cost nothing: the small ones fit.
    monkeypatch.setattr(C, "clone", lambda src, out, path: os.write(out, os.pread(src, 3 << 20, 0)))
    C.Store.of(str(ws)).reflink = None
    meta = C.take(str(ws), roots(ws))
    assert meta["files"] == 4 and meta["reflink"] is True


def test_a_new_checkpoint_keeps_the_start_checkpoint_of_every_run_that_runs(tmp_path, monkeypatch):
    """Review 3x-3: "the start checkpoints of running runs always stay" - but the limits applied after a new
    checkpoint protected only the start checkpoint of the run that took it: another run of the same project that
    still runs lost its own (restore NAME then had nothing to go back to). Only `sandbox-ai gc` knew them all."""
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f").write_text("a")
    first = C.take(str(ws), roots(ws), kind="start", run="run-a", prune_after=False)
    registered = tmp_path / "home/.local/state/sandbox-ai/runs/run-a"
    registered.mkdir(parents=True)
    (registered / "run.json").write_text(json.dumps({"run": "run-a", "pid": os.getpid()}))  # its launcher runs
    defaults = tmp_path / "home/.config/sandbox-ai/defaults.json"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(json.dumps({"checkpoint_count": 2}))
    for i in range(3):
        (ws / "f").write_text(f"b{i}")
        C.take(str(ws), roots(ws), kind="manual", run="run-b")
    kept = [m["id"] for m in C.Store.of(str(ws)).metas()]
    assert first["id"] in kept, "the start checkpoint of the run that runs stays"
    assert len(kept) == 2  # the limit holds: the newest and the protected one
    (registered / "run.json").unlink()  # run-a ended: its start checkpoint counts like any other
    (ws / "f").write_text("c")
    C.take(str(ws), roots(ws), kind="manual", run="run-b")
    assert first["id"] not in [m["id"] for m in C.Store.of(str(ws)).metas()]


# --- review 3x-1 (security): what an agent does to a path must not take it out of the undo ---------------------------

def test_a_path_made_a_special_file_since_is_restored_all_the_same(tmp_path):
    """Review 3x-1: a path the checkpoint taken before a restore skips (a FIFO, a socket: "not checkpointed") was
    held back from the restore - an agent that replaced a file, a folder or .git by a FIFO of that name kept it out
    of `checkpoint restore`, without a word. Only what the checkpoint being restored did not hold stays as it is."""
    ws = workspace(tmp_path)
    os.mkfifo(ws / "was-there")  # a special file of before: never checkpointed, never touched
    before = tree(ws)
    meta = C.take(str(ws), roots(ws), kind="start")
    import shutil
    (ws / "run.sh").unlink()
    os.mkfifo(ws / "run.sh")  # a file -> a FIFO
    shutil.rmtree(ws / "src")
    os.mkfifo(ws / "src")  # a folder with files -> a FIFO
    shutil.rmtree(ws / ".git")
    os.mkfifo(ws / ".git")
    os.mkfifo(ws / "new-pipe")  # a special file that was not there
    (ws / "empty" / "x").write_text("added")
    found = C.diff(meta["id"])
    changed = {rel: what for root in found["roots"] for rel, what in root["changed"]}
    assert changed.get("run.sh") == "type" and changed.get("src") == "type" and changed.get(".git") == "type"
    assert "new-pipe" in [rel for root in found["roots"] for rel in root["added"]]
    result = C.restore(meta["id"])
    assert tree(ws) == before, "everything as it was: the FIFOs of the agent are gone, the one of before stays"
    assert result["counts"]["changed"] >= 3 and result["counts"]["added"] >= 2


UNREADABLE = r'''
import importlib.util, json, os, shutil, sys
from pathlib import Path
root, base = sys.argv[1], Path(sys.argv[2])
if os.geteuid() == 0:
    # Root reads and writes whatever the permission bits say: this process gives that up (CAP_DAC_OVERRIDE,
    # CAP_DAC_READ_SEARCH leave its effective and permitted sets) - it is root as an owner is.
    import ctypes

    class Header(ctypes.Structure):
        _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]

    class Data(ctypes.Structure):
        _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32), ("inheritable", ctypes.c_uint32)]
    libc = ctypes.CDLL(None, use_errno=True)
    header, data = Header(0x20080522, 0), (Data * 2)()
    if libc.capget(ctypes.byref(header), data) != 0:
        sys.exit(77)
    for field in ("effective", "permitted", "inheritable"):
        setattr(data[0], field, getattr(data[0], field) & ~0b110)
    if libc.capset(ctypes.byref(header), data) != 0:
        sys.exit(77)
    probe = base / "probe"
    probe.mkdir()
    os.chmod(probe, 0)
    try:
        os.listdir(probe)
        sys.exit(77)
    except PermissionError:
        os.rmdir(probe)
os.environ.update(HOME=str(base / "home"), XDG_STATE_HOME=str(base / "home/state"), XDG_CONFIG_HOME=str(base / "home/config"))
spec = importlib.util.spec_from_file_location("checkpoints_unreadable", root + "/sandbox/checkpoints.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)
ws = base / "ws"
(ws / "src/pkg").mkdir(parents=True)
(ws / "src/pkg/a.py").write_text("good\n")
(ws / "src/b.txt").write_text("good\n")
(ws / "src/node_modules/dep").mkdir(parents=True)   # excluded: never checkpointed, never touched by a restore
(ws / "src/node_modules/dep/index.js").write_text("built\n")
(ws / "locked").mkdir()
(ws / "locked/f").write_text("good\n")
(ws / "kept-private").mkdir()
(ws / "kept-private/mine").write_text("mine\n")
os.chmod(ws / "kept-private", 0)          # unreadable before: never checkpointed, never touched
os.chmod(ws / "src", 0o750)
meta = C.take(str(ws), [{"host": str(ws), "sandbox": "/work", "kind": "workspace"}], kind="start")
# the agent: trash, then lock the folders (no permission for their owner)
(ws / "src/pkg/a.py").write_text("evil\n")
(ws / "src/planted.sh").write_text("evil\n")
os.chmod(ws / "src/pkg", 0)
os.chmod(ws / "src", 0)
(ws / "locked/f").write_text("evil\n")
os.chmod(ws / "locked", 0o500)             # readable, nothing may be written into it
C.restore(meta["id"])
out = {"a": (ws / "src/pkg/a.py").read_text(), "b": (ws / "src/b.txt").read_text(),
       "planted": (ws / "src/planted.sh").exists(), "f": (ws / "locked/f").read_text(),
       "src": oct(os.stat(ws / "src").st_mode & 0o777), "locked": oct(os.stat(ws / "locked").st_mode & 0o777),
       "private": oct(os.stat(ws / "kept-private").st_mode & 0o777),
       "excluded": (ws / "src/node_modules/dep/index.js").read_text()
       if (ws / "src/node_modules/dep/index.js").exists() else None}
os.chmod(ws / "kept-private", 0o700)
out["mine"] = (ws / "kept-private/mine").read_text()
print(json.dumps(out))
'''


def test_a_folder_the_agent_locked_is_restored_all_the_same(tmp_path):
    """Review 3x-1: `chmod 000 src` after changing what is in it: the checkpoint before the restore cannot read the
    folder ("cannot be read"), which held it back from the restore; one left without write permission ended the
    restore with an error. The folder's owner restores it: its permissions are the checkpoint's afterwards. (Root
    reads whatever the bits say: the scenario runs without that capability then.)"""
    import subprocess
    root = os.geteuid() == 0
    if root and not sys.platform.startswith("linux"):
        pytest.skip("as root nothing is unreadable")
    try:
        done = subprocess.run([sys.executable, "-I", "-c", UNREADABLE, str(ROOT), str(tmp_path)], capture_output=True,
                              text=True, timeout=120)
    finally:
        subprocess.run(["chmod", "-R", "u+rwx", str(tmp_path / "ws")], capture_output=True)
    if done.returncode == 77:
        pytest.skip("this root cannot give up its way past the permission bits")
    assert done.returncode == 0, done.stderr[-2000:]
    assert json.loads(done.stdout) == {"a": "good\n", "b": "good\n", "planted": False, "f": "good\n",
                                       "src": "0o750", "locked": "0o755", "private": "0o0", "mine": "mine\n",
                                       "excluded": "built\n"}


def test_a_mode_is_never_changed_through_a_link_swapped_in(tmp_path, monkeypatch):
    """Review 3x-1: a file its owner may not read has its mode put back by name; a link swapped in at that moment
    was followed (os.chmod) - to a file of the host, which got the mode the agent chose."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f").write_text("x")
    victim = tmp_path / "victim"
    victim.write_text("host")
    victim.chmod(0o600)
    store = C.Store.of(str(ws))
    restorer = C.Restorer(store, str(ws), "dir")
    real_open, real_stat = os.open, os.stat

    def no_permission(path, flags, *args, **kwargs):
        if path == "f" and kwargs.get("dir_fd") is not None:
            raise PermissionError(13, "Permission denied")
        return real_open(path, flags, *args, **kwargs)

    def swapped(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if path == "f" and kwargs.get("dir_fd") is not None:  # checked: a file - then the agent swaps a link in
            (ws / "f").unlink()
            os.symlink(victim, ws / "f")
        return info
    monkeypatch.setattr(os, "open", no_permission)
    monkeypatch.setattr(os, "stat", swapped)
    restorer.chmod("f", 0o666)
    monkeypatch.undo()
    assert stat.S_IMODE(victim.stat().st_mode) == 0o600, "the mode went through the link to a file of the host"
