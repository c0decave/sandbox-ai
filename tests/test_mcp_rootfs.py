"""Catalog MCP servers under Bubblewrap (sandbox/mcp_rootfs.py): the archive checked against the signed SHA-256 and
unpacked without trusting it, the definition checked, bwrap's arguments as hardened as the container's run line
and kept off every command line, and `sandbox-ai mcp add --backend bwrap` to a running bridge. bwrap itself runs in
the live tests (tests/test_live_e2e.py) and in sandbox-ai-mcp's CI."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tarfile

import pytest

import test_mcp as tm
from test_mcp import isolated, mcp_key, trusted_mcp_key  # noqa: F401  (fixtures)

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("mcp_rootfs_for_tests", ROOT / "sandbox" / "mcp_rootfs.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
m = tm.m


@pytest.fixture(autouse=True)
def data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    m.rootfs_module()  # the module mcp.py uses reads the same variable
    yield tmp_path / "data"
    # Unpacked trees are read-only (as installed): writable again, or pytest cannot remove tmp_path, keeps every
    # run's whole basetemp as garbage-* and fills /tmp (a tmpfs in the sandbox: memory).
    for directory, dirs, _ in os.walk(tmp_path):
        for name in dirs:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                os.chmod(child, 0o700)


def archive(*members):
    """A tar.gz of MEMBERS: (name, kind, content or link target, mode)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, kind, value, mode in members:
            info = tarfile.TarInfo(name)
            info.mode = mode
            if kind == "file":
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
            else:
                info.type = {"dir": tarfile.DIRTYPE, "sym": tarfile.SYMTYPE, "link": tarfile.LNKTYPE,
                             "chr": tarfile.CHRTYPE, "fifo": tarfile.FIFOTYPE}[kind]
                if kind in ("sym", "link"):
                    info.linkname = value
                tar.addfile(info)
    return buffer.getvalue()


GOOD = archive(("usr", "dir", None, 0o755), ("usr/bin", "dir", None, 0o755),
               ("usr/bin/server", "file", b"#!/bin/sh\necho hi\n", 0o4755),
               ("usr/bin/link", "sym", "/usr/bin/server", 0o777), ("usr/bin/hard", "link", "usr/bin/server", 0o755),
               ("etc", "dir", None, 0o755), ("etc/conf", "file", b"x=1\n", 0o666),
               ("dev", "dir", None, 0o755), ("dev/null", "chr", None, 0o666), ("run/fifo", "fifo", None, 0o644))


def test_a_good_archive_unpacks_read_only_without_setuid(data_home):
    sha = hashlib.sha256(GOOD).hexdigest()
    source = data_home.parent / "rootfs.tar.gz"
    source.write_bytes(GOOD)
    tree = r.install(str(source), sha)
    assert tree == r.rootfs_dir(sha) and (tree / "usr/bin/server").read_bytes().startswith(b"#!/bin/sh")
    mode = (tree / "usr/bin/server").stat().st_mode
    assert not mode & (stat.S_ISUID | stat.S_ISGID) and mode & 0o111 and not mode & 0o222, oct(mode)
    assert not (tree / "etc/conf").stat().st_mode & 0o222, "nothing in it writable"
    assert os.readlink(tree / "usr/bin/link") == "/usr/bin/server", "a link kept, resolved only inside bwrap's root"
    assert (tree / "usr/bin/hard").stat().st_ino == (tree / "usr/bin/server").stat().st_ino
    assert not (tree / "dev/null").exists() and not (tree / "run/fifo").exists(), "no devices or FIFOs"
    assert r.install(str(source), sha) == tree, "a second install takes the tree there is"
    r.remove(tree)
    assert not tree.exists()


@pytest.mark.parametrize("members, why", [
    ([("../escape", "file", b"x", 0o644)], "leaves the tree"),
    ([("a/../../escape", "file", b"x", 0o644)], "leaves the tree"),
    ([("etc", "sym", "/etc", 0o777), ("etc/passwd", "file", b"x", 0o644)], "no directory"),
    ([("out", "sym", "../../..", 0o777), ("out/x", "file", b"x", 0o644)], "no directory"),
    ([("h", "link", "../outside", 0o644)], "leaves the tree"),
    ([("l", "sym", "/etc/passwd", 0o777), ("h", "link", "l", 0o644)], "no file of it"),
    ([("d", "dir", None, 0o755), ("d", "file", b"x", 0o644)], "a file where a directory is"),
])
def test_an_archive_that_tries_to_leave_is_refused(data_home, members, why):
    data = archive(*members)
    target = data_home / "t"
    data_home.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match=why):
        r.unpack(data, target)
    assert not target.exists() and not list(data_home.glob(".unpack-*")), "nothing half unpacked is left"
    assert not (data_home.parent / "escape").exists() and not (data_home / "escape").exists()


def test_a_double_slash_root_stays_inside_the_tree(data_home, tmp_path):
    """POSIX keeps a leading // as a root of its own: such a member, or a hard link to one, named the host's own
    files (written over, or linked into the tree) before."""
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "secret").write_text("key")
    (victim / "secret").chmod(0o600)
    data_home.mkdir(parents=True, exist_ok=True)
    data = archive((f"/{victim}/written", "file", b"x", 0o644), (f"///{victim}/also", "file", b"y", 0o644))
    tree = r.unpack(data, data_home / "t")
    assert not (victim / "written").exists() and not (victim / "also").exists()
    assert (tree / str(victim).lstrip("/") / "written").read_bytes() == b"x"
    with pytest.raises(ValueError, match="no file of it"):
        r.unpack(archive(("stolen", "link", f"/{victim}/secret", 0o644)), data_home / "u")
    assert (victim / "secret").stat().st_nlink == 1 and stat.S_IMODE((victim / "secret").stat().st_mode) == 0o600
    assert r.safe_name("//etc/x") == r.PurePosixPath("etc/x") and r.safe_name("/") is None
    with pytest.raises(ValueError, match="leaves the tree"):
        r.directory_of(data_home, r.PurePosixPath("//etc/x"))


def test_the_archive_must_match_the_signed_hash(data_home, tmp_path, monkeypatch):
    source = tmp_path / "rootfs.tar.gz"
    source.write_bytes(GOOD)
    with pytest.raises(ValueError, match="the signed catalog says"):
        r.install(str(source), "0" * 64)
    assert not r.rootfs_dir("0" * 64).exists()
    with pytest.raises(ValueError, match="https URL or a local file"):
        r.fetch("http://example.org/rootfs.tar.gz", "0" * 64)
    with pytest.raises(ValueError, match="64 hex"):
        r.rootfs_dir("../../x")
    monkeypatch.setattr(r, "UNPACKED_LIMIT", 10)
    with pytest.raises(ValueError, match="unpacks to more than"):
        r.unpack(GOOD, data_home / "big")


def definition(tmp_path, network="none", env=(), mounts=()):
    sha = "a" * 64
    return {"rootfs": {"path": str(r.rootfs_dir(sha)), "sha256": sha, "entrypoint": ["/opt/venv/bin/python", "-I",
                                                                                   "/opt/mcp/server.py"],
                       "user": 10001, "workdir": "/tmp", "env": {"HOME": "/tmp"}},
            "network": network, "env": list(env), "mounts": list(mounts), "memory": "512m", "pids": 128}


@pytest.mark.parametrize("change, why", [
    ({"user": 0}, "never root"), ({"entrypoint": ["python3"]}, "absolute program"), ({"entrypoint": []}, "absolute"),
    ({"path": "/tmp/elsewhere"}, "its place"), ({"sha256": "x"}, "64 hex"), ({"workdir": "../x"}, "absolute"),
    ({"env": {"A=B": "1"}}, "variable names"), ({"extra": 1}, "rootfs is"),
])
def test_what_a_rootfs_may_not_be(tmp_path, change, why):
    rootfs = {**definition(tmp_path)["rootfs"], **change}
    with pytest.raises(ValueError, match=why):
        r.validated(rootfs, "t")


def test_bwrap_is_as_hardened_as_the_container(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    d = definition(tmp_path, env=["API_KEY"], mounts=[{"target": "/work", "host": str(work), "mode": "ro"},
                                                     {"target": "/out", "host": str(work), "mode": "rw"}])
    args = r.bwrap_args(d, {"API_KEY": "s3cr3t", "OTHER": "never"})
    for flag in ("--die-with-parent", "--new-session", "--unshare-user", "--unshare-ipc", "--unshare-pid",
                 "--unshare-uts", "--unshare-net", "--clearenv"):
        assert flag in args, flag
    joined = " ".join(args)
    assert f"--ro-bind {d['rootfs']['path']} /" in joined and "--cap-drop ALL" in joined
    assert "--uid 10001 --gid 10001" in joined and "--tmpfs /tmp" in joined and "--proc /proc --dev /dev" in joined
    assert f"--ro-bind {work} /work" in joined and f"--bind {work} /out" in joined
    assert "--setenv API_KEY s3cr3t" in joined and "OTHER" not in joined and "--setenv HOME /tmp" in joined
    # bwrap takes only options from --args FD (a command after "--" there was dropped: no server ever started); the
    # command goes on bwrap's own command line after it.
    assert "--" not in args and args[-2:] == ["--chdir", d["rootfs"].get("workdir", "/tmp")]
    assert r.entrypoint(d) == ["--", "/opt/venv/bin/python", "-I", "/opt/mcp/server.py"]
    assert "resolv.conf" not in joined, "no network: no resolver either"
    full = r.bwrap_args(definition(tmp_path, network="full"), {})
    assert "--unshare-net" in full and any(a.endswith("resolv.conf") for a in full), "slirp4netns's own network"


def test_secrets_stay_off_every_command_line(tmp_path):
    d = definition(tmp_path, env=["API_KEY"])
    command = r.bwrap_command(tmp_path / "definition.json", d)
    assert command[-2:] == ["--internal-bwrap", str(tmp_path / "definition.json")]
    assert "s3cr3t" not in " ".join(command) and "bwrap" not in command[0]
    fd = r.args_pipe(["--setenv", "API_KEY", "s3cr3t", "--", "/x"])
    try:
        assert os.read(fd, 1000) == b"--setenv\0API_KEY\0s3cr3t\0--\0/x\0" and os.get_inheritable(fd)
    finally:
        os.close(fd)


def test_add_with_bubblewrap_and_start_the_bridge(tmp_path, data_home, monkeypatch):
    source = tmp_path / "rootfs.tar.gz"
    source.write_bytes(GOOD)
    sha = hashlib.sha256(GOOD).hexdigest()
    entry = {**tm.ENTRY, "network": "none", "hosts": []}
    sources = tm.catalog(tmp_path, [entry])
    rootfs = {"url": str(source), "sha256": sha, "entrypoint": ["/usr/bin/server"], "user": 10001,
              "workdir": "/tmp", "env": {"HOME": "/tmp"}}
    (tmp_path / "rootfs.json").write_text(json.dumps({"version": 1, "rootfs": {"duckduckgo": rootfs}}))
    # Beside a signed catalog an unsigned rootfs.json is not used.
    with pytest.raises(ValueError, match="no rootfs for Bubblewrap"):
        m.add("duckduckgo", catalogs=sources, out=lambda line: None, backend="bwrap")
    tm.sign(tmp_path / "rootfs.json")
    definition = m.add("duckduckgo", catalogs=sources, out=lambda line: None, backend="bwrap")
    assert "image" not in definition and definition["rootfs"]["sha256"] == sha and definition["signed_by"]
    assert Path(definition["rootfs"]["path"]).is_dir()
    # Without a rootfs in the catalog, Bubblewrap is refused (Podman stays possible).
    (tmp_path / "plain").mkdir()
    plain = tm.catalog(tmp_path / "plain", [tm.ENTRY])
    with pytest.raises(ValueError, match="no rootfs for Bubblewrap"):
        m.add("duckduckgo", network="full", catalogs=plain, out=lambda line: None, backend="bwrap")
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    tm.fake_supervisor(tmp_path, os.getpid(), "duckduckgo", start=4242)
    assert m.ensure_running("duckduckgo", tm.s.parse_args(["codex", "--backend", "bwrap"])) == "started"
    cmd = started[-1]
    assert "--internal-mcp-serve" in cmd and "--internal-bwrap" in cmd and "podman" not in cmd
    checked = Path(cmd[-1])
    assert json.loads(checked.read_text())["rootfs"]["sha256"] == sha and checked.stat().st_mode & 0o077 == 0
    assert m.load_state("duckduckgo")["backend"] == "bwrap"


@pytest.mark.parametrize("network", ["none", "full"])
def test_bwrap_gets_the_command_on_its_command_line_after_the_args_pipe(tmp_path, monkeypatch, network):
    d = definition(tmp_path, env=["API_KEY"], network=network)
    seen = []

    class Done(Exception):
        pass

    def execvp(program, argv):
        seen.append(argv)
        raise Done

    def popen(argv, **kw):
        seen.append(argv)
        raise Done
    monkeypatch.setattr(r.os, "execvp", execvp)
    monkeypatch.setattr(r.subprocess, "Popen", popen)
    try:
        r.run(d, {"API_KEY": "s3cr3t"})
    except Done:
        pass
    argv = seen[0]
    assert argv[0] == "bwrap" and "--args" in argv
    assert argv[argv.index("--"):] == ["--", "/opt/venv/bin/python", "-I", "/opt/mcp/server.py"]
    assert "s3cr3t" not in " ".join(argv), "variables stay in the pipe"
