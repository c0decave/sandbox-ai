"""remote.py: a folder of a remote system over SSH and SFTP, as a Root of the Files page. Against a fake SFTP server
(fake_sftp_server.py, this machine's files) and, where one is found, OpenSSH's own sftp-server: targets and ssh's
options, what ssh's messages mean, known hosts, listing, reading and writing, links never followed, budgets, timeouts,
and copies and moves between a remote folder and local ones. No Qt."""
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys

import pytest

from sandbox_ai_app import files, remote

HERE = Path(__file__).resolve().parent
FAKE = [sys.executable, "-I", str(HERE / "fake_sftp_server.py")]


def sftp_server():
    """OpenSSH's sftp-server: $SANDBOX_AI_TEST_SFTP_SERVER or where distributions put it."""
    for path in (os.environ.get("SANDBOX_AI_TEST_SFTP_SERVER"), "/usr/lib/openssh/sftp-server",
                 "/usr/libexec/sftp-server", "/usr/lib/ssh/sftp-server", "/usr/libexec/openssh/sftp-server"):
        if path and os.access(path, os.X_OK):
            return path
    return None


# -- targets and ssh --

def test_targets_are_parsed_and_anything_else_refused():
    assert remote.Target.parse("alice@example.org:2222") == remote.Target("example.org", "alice", 2222)
    assert remote.Target.parse("build-box") == remote.Target("build-box")
    assert remote.Target.parse("[::1]:22").host == "::1" and remote.Target.parse("[::1]:22").text() == "[::1]:22"
    for bad in ("-oProxyCommand=sh", "a b", "host;rm -rf /", "user@host:99999", "", "x@-y", "host\nx", "$(id)"):
        with pytest.raises(files.Refused):
            remote.Target.parse(bad)


def test_ssh_runs_without_a_shell_in_batch_mode_and_with_strict_host_keys(tmp_path):
    target = remote.Target("example.org", "alice", 2222, "~/.ssh/id_work")
    args = target.ssh_args(known_hosts=str(tmp_path / "kh"))
    joined = " ".join(args)
    assert "BatchMode=yes" in joined and "StrictHostKeyChecking=yes" in joined and "ForwardAgent=no" in joined
    assert ["-p", "2222"] == args[args.index("-p"):args.index("-p") + 2] and ["-l", "alice"] == \
        args[args.index("-l"):args.index("-l") + 2]
    assert f"UserKnownHostsFile={tmp_path / 'kh'}" in args and os.path.expanduser("~/.ssh/id_work") in args
    interactive = remote.ssh_command(target)
    assert "BatchMode=yes" not in interactive and interactive[-2:] == ["--", "example.org"]
    assert all(interactive[i + 1] != "BatchMode=yes" for i, a in enumerate(interactive[:-1]) if a == "-o")


def test_what_ssh_says_when_it_fails():
    target = remote.Target("h")
    assert isinstance(remote.diagnose("Host key verification failed.\n", target), remote.HostKeyUnknown)
    assert isinstance(remote.diagnose("No ED25519 host key is known for h and you have requested strict checking.\n"
                                      "Host key verification failed.", target), remote.HostKeyUnknown)
    changed = remote.diagnose("@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @\nHost key verification failed.",
                              target)
    assert isinstance(changed, remote.HostKeyChanged), "a changed key is never offered to be added"
    assert isinstance(remote.diagnose("alice@h: Permission denied (publickey).", target), remote.AuthenticationNeeded)
    other = remote.diagnose("ssh: Could not resolve hostname h: Name or service not known", target)
    assert type(other) is remote.RemoteError and "Could not resolve" in str(other)


def test_a_failing_ssh_is_said_in_words(tmp_path):
    fake_ssh = ["sh", "-c", "echo 'Host key verification failed.' >&2; exit 255"]
    with pytest.raises(remote.HostKeyUnknown):
        remote.RemoteRoot(remote.Target("h"), ssh=fake_ssh)
    with pytest.raises(remote.AuthenticationNeeded):
        remote.RemoteRoot(remote.Target("h"), ssh=["sh", "-c", "echo 'u@h: Permission denied (publickey).' >&2"])
    with pytest.raises(remote.RemoteError, match="not speak SFTP|ended|damaged"):
        remote.RemoteRoot(remote.Target("h"), ssh=["sh", "-c", "printf 'hello, a shell banner\\n'; cat"])


def test_known_hosts_and_ssh_config(tmp_path):
    path = tmp_path / "ssh" / "known_hosts"
    remote.add_known_host(["h ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAa"], str(path))
    assert path.read_text() == "h ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIAa\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600 and stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    with pytest.raises(files.Refused):
        remote.add_known_host(["h ssh-ed25519 X\nevil ssh-rsa Y"], str(path))
    config = tmp_path / "config"
    config.write_text("Host work build-*\n  HostName 10.0.0.2\nHost *\n  User me\nhost nas !bad\n")
    assert remote.ssh_config_hosts(str(config)) == ["work", "nas"]
    keygen = shutil.which("ssh-keygen")
    if keygen:
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "key")], check=True)
        line = "h " + (tmp_path / "key.pub").read_text().split(" ", 2)[0] + " " + \
            (tmp_path / "key.pub").read_text().split(" ", 2)[1]
        found = remote.fingerprints([line], keygen)
        assert len(found) == 1 and found[0].startswith("ED25519 SHA256:")
    scanner = tmp_path / "keyscan"
    scanner.write_text("#!/bin/sh\necho '# h:22 SSH-2.0'\necho 'h ssh-ed25519 AAAA'\n")
    scanner.chmod(0o755)
    assert remote.scan_host(remote.Target("h"), str(scanner)) == ["h ssh-ed25519 AAAA"]


# -- the root, against the fake server --

@pytest.fixture
def tree(tmp_path):
    served = tmp_path / "served"
    (served / "docs").mkdir(parents=True)
    (served / "docs" / "a.txt").write_text("alpha")
    (served / "run.sh").write_text("#!/bin/sh\n")
    (served / "run.sh").chmod(0o755)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("the host's own")
    (served / "escape").symlink_to(tmp_path / "outside")
    (served / "link.txt").symlink_to(served / "docs" / "a.txt")
    return served


def connect(path, server=None, **env):
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        return remote.RemoteRoot(remote.Target("fake"), str(path), ssh=server or FAKE, timeout=5)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_listing_reading_and_links_never_followed(tree, tmp_path):
    root = connect(tree)
    try:
        assert root.path == str(tree.resolve()) and root.key == f"ssh://fake{tree.resolve()}"
        listing = root.list("")
        assert [(e.name, e.kind) for e in listing] == [("docs", "dir"), ("escape", "link"), ("link.txt", "link"),
                                                        ("run.sh", "file")], "folders first"
        assert next(e for e in listing if e.name == "escape").target == str(tmp_path / "outside")
        assert [e.name for e in root.list("docs")] == ["a.txt"]
        with pytest.raises(files.Refused, match="symbolic link to .*outside; it is not followed"):
            root.list("escape")
        with pytest.raises(files.Refused, match="not followed"):
            root.read_file("escape/secret.txt")
        with pytest.raises(files.Refused, match="symbolic link"):
            root.read_file("link.txt")
        with pytest.raises(ValueError):
            root.list("../outside")
        data = root.read_file("docs/a.txt")
        assert data.data == b"alpha" and not data.truncated and data.size == 5
        assert root.read_file("docs/a.txt", limit=2).data == b"al" and root.read_file("docs/a.txt", limit=2).truncated
        assert root.stat_entry("escape").st_mode & stat.S_IFLNK and root.readlink("escape") == str(tmp_path / "outside")
        assert root.measure("").files == 4 and root.measure("docs").bytes == 5
        free, total = root.free_space("docs")
        assert 0 <= free <= total and total == os.statvfs(tree).f_blocks * (os.statvfs(tree).f_frsize or 1)
    finally:
        root.close()


def test_writing_renaming_making_and_deleting(tree, tmp_path):
    root = connect(tree)
    try:
        before = root.read_file("docs/a.txt")
        (tree / "docs" / "a.txt").chmod(0o640)
        before = root.read_file("docs/a.txt")
        root.write_file("docs/a.txt", b"beta", expected=before.identity)
        assert (tree / "docs" / "a.txt").read_text() == "beta" and stat.S_IMODE((tree / "docs" / "a.txt").stat().st_mode) == 0o640
        (tree / "docs" / "a.txt").write_text("the other program's")
        with pytest.raises(files.Changed):
            root.write_file("docs/a.txt", b"mine", expected=before.identity)
        assert not [p for p in (tree / "docs").iterdir() if p.name.endswith(".part")], "no temporary file left"
        root.make_folder("docs", "new")
        root.make_file("docs/new", "empty.md")
        assert (tree / "docs" / "new" / "empty.md").read_bytes() == b""
        for make in (lambda: root.make_folder("docs", "new"), lambda: root.make_file("docs/new", "empty.md"),
                     lambda: root.make_file("", "escape")):
            with pytest.raises(files.Refused, match="already exists"):
                make()
        root.rename_entry("docs/new/empty.md", "renamed.md")
        assert (tree / "docs" / "new" / "renamed.md").exists()
        root.chmod_entry("run.sh", 0o700)
        assert stat.S_IMODE((tree / "run.sh").stat().st_mode) == 0o700
        with pytest.raises(files.Refused, match="no permissions of its own"):
            root.chmod_entry("escape", 0o777)
        with pytest.raises(files.Refused):
            root.chown_entry("run.sh", 0, 0)
        assert root.delete_entry("escape").removed == 1 and not os.path.lexists(tree / "escape")
        assert (tmp_path / "outside" / "secret.txt").exists(), "the link removed, never what it points to"
        assert root.delete_entry("docs").removed == 4 and not (tree / "docs").exists()
        with pytest.raises(files.Refused):
            root.delete_entry("")
    finally:
        root.close()


def test_names_with_line_breaks_are_never_sent(tree, tmp_path):
    log = tmp_path / "requests.log"
    root = connect(tree, FAKE_SFTP_LOG=str(log))
    try:
        sent = log.read_text().count("\n")
        for bad in ("a\nb", "c\rd", "e\0f"):
            with pytest.raises((files.Refused, ValueError)):
                root.make_folder("", bad)
            with pytest.raises((files.Refused, ValueError)):
                root.read_file(bad)
        assert log.read_text().count("\n") == sent, "nothing sent for them"
    finally:
        root.close()


def test_a_request_without_an_answer_ends_the_connection(tree):
    root = connect(tree, FAKE_SFTP_HANG="READDIR")
    root.sftp.timeout = 1
    with pytest.raises(remote.ConnectionLost, match="did not answer within 1 s"):
        root.list("docs")
    assert not root.alive


def test_without_extensions_free_space_is_not_known_and_nothing_is_replaced_in_one_step(tree):
    root = connect(tree, FAKE_SFTP_NO_EXTENSIONS="1")
    try:
        with pytest.raises(OSError):
            root.free_space("")
        with pytest.raises(OSError):
            root.write_file("docs/a.txt", b"x")
        assert (tree / "docs" / "a.txt").read_text() == "alpha"
        assert not [p for p in (tree / "docs").iterdir() if p.name.endswith(".part")]
    finally:
        root.close()


# -- copies and moves with a remote side --

@pytest.fixture
def local(tmp_path):
    host = tmp_path / "host"
    host.mkdir()
    sandbox = tmp_path / "ws"
    sandbox.mkdir()
    return files.Root(host, sandbox=False), files.Root(sandbox), host, sandbox


def test_copies_from_a_remote_folder_skip_links_and_leave_no_staging(tree, local):
    host_root, sandbox_root, host, sandbox = local
    (tree / "docs" / "inner").symlink_to("/etc")
    (tree / "docs" / "tool.sh").write_text("echo")
    (tree / "docs" / "tool.sh").chmod(0o755)
    root = connect(tree)
    try:
        result = files.copy_entry(root, "docs", host_root, "")
        assert (host / "docs" / "a.txt").read_text() == "alpha" and not os.path.lexists(host / "docs" / "inner")
        assert [path for path, _ in result.skipped] == ["docs/inner"]
        assert os.access(host / "docs" / "tool.sh", os.X_OK), "into the host: as it was"
        assert not [p for p in host.iterdir() if p.name.endswith(".part")], "the staging folder is gone"
        result = files.copy_entry(root, "docs", sandbox_root, "")
        assert not os.access(sandbox / "docs" / "tool.sh", os.X_OK), "into a sandbox's folder: as the agent's"
        assert "docs/tool.sh" in result.executable
        with pytest.raises(files.Refused, match="already exists"):
            files.copy_entry(root, "docs", host_root, "")
        (tree / "docs" / "a.txt").write_text("newer")
        files.copy_entry(root, "docs", host_root, "", overwrite=True)
        assert (host / "docs" / "a.txt").read_text() == "newer"
        with pytest.raises(files.Refused, match="symbolic link"):
            files.copy_entry(root, "escape", host_root, "")
        with pytest.raises(files.Refused, match="More than"):
            files.copy_entry(root, "docs", host_root, "", name="small", limit=3)
        assert not (host / "small").exists() and not [p for p in host.iterdir() if p.name.endswith(".part")]
        assert files.exists_in(root, "docs", ["a.txt", "nope"]) == ["a.txt"]
    finally:
        root.close()


def test_copies_to_a_remote_folder_go_in_whole_or_not_at_all(tree, local):
    host_root, sandbox_root, host, sandbox = local
    (sandbox / "proj").mkdir()
    (sandbox / "proj" / "x.sh").write_text("x")
    (sandbox / "proj" / "x.sh").chmod(0o755)
    (sandbox / "proj" / "loop").symlink_to("/")
    (host / "notes.txt").write_text("notes")
    root = connect(tree)
    try:
        result = files.copy_entry(sandbox_root, "proj", root, "docs")
        assert (tree / "docs" / "proj" / "x.sh").read_text() == "x"
        assert not os.access(tree / "docs" / "proj" / "x.sh", os.X_OK), "a sandbox's file never arrives executable"
        assert result.executable == ["proj/x.sh"] and [p for p, _ in result.skipped] == ["proj/loop"]
        files.copy_entry(host_root, "notes.txt", root, "")
        assert (tree / "notes.txt").read_text() == "notes"
        with pytest.raises(files.Refused, match="already exists"):
            files.copy_entry(host_root, "notes.txt", root, "")
        (host / "notes.txt").write_text("newer")
        files.copy_entry(host_root, "notes.txt", root, "", overwrite=True)
        assert (tree / "notes.txt").read_text() == "newer"
        # Cancelled half-way: nothing of it stays.
        for number in range(5):
            (host / "many").mkdir(exist_ok=True)
            (host / "many" / f"f{number}").write_text("y" * 1000)
        calls = []
        with pytest.raises(files.Cancelled):
            files.copy_entry(host_root, "many", root, "", cancelled=lambda: calls.append(1) or len(calls) > 4)
        assert not (tree / "many").exists() and not [p for p in tree.iterdir() if p.name.endswith(".part")]
        with pytest.raises(files.Refused, match="More than"):
            files.copy_entry(host_root, "many", root, "", limit=100)
        assert not (tree / "many").exists()
    finally:
        root.close()


def test_moves_with_a_remote_side(tree, local):
    host_root, sandbox_root, host, sandbox = local
    root = connect(tree)
    try:
        result = files.move_entry(root, "docs/a.txt", host_root, "")
        assert result.moved and (host / "a.txt").read_text() == "alpha" and not (tree / "docs" / "a.txt").exists()
        files.move_entry(host_root, "a.txt", root, "docs")
        assert (tree / "docs" / "a.txt").exists() and not (host / "a.txt").exists()
        (tree / "other").mkdir()
        files.move_entry(root, "docs/a.txt", root, "other")  # within one remote system: a rename
        assert (tree / "other" / "a.txt").read_text() == "alpha"
        with pytest.raises(files.Refused, match="into itself"):
            files.move_entry(root, "other", root, "other")
        with pytest.raises(files.Refused, match="not made here"):
            files.copy_entry(root, "other", root, "docs")
    finally:
        root.close()


# -- OpenSSH's own sftp-server --

def test_against_openssh_sftp_server(tree, local):
    server = sftp_server()
    if server is None:
        pytest.skip("OpenSSH's sftp-server is not installed (openssh-sftp-server, or $SANDBOX_AI_TEST_SFTP_SERVER)")
    host_root, sandbox_root, host, sandbox = local
    root = connect(tree, [server, "-e"])
    try:
        assert [e.name for e in root.list("")] == ["docs", "escape", "link.txt", "run.sh"]
        assert root.list("")[1].target is not None and root.free_space("")[1] > 0
        root.write_file("docs/a.txt", b"written over sftp", expected=root.read_file("docs/a.txt").identity)
        assert (tree / "docs" / "a.txt").read_text() == "written over sftp"
        files.copy_entry(root, "docs", host_root, "")
        assert (host / "docs" / "a.txt").read_text() == "written over sftp"
        (host / "big.bin").write_bytes(os.urandom(300_000))
        files.copy_entry(host_root, "big.bin", root, "docs")
        assert (tree / "docs" / "big.bin").read_bytes() == (host / "big.bin").read_bytes(), "pipelined writes"
        files.copy_entry(root, "docs/big.bin", sandbox_root, "")
        assert (sandbox / "big.bin").read_bytes() == (host / "big.bin").read_bytes(), "pipelined reads"
        with pytest.raises(files.Refused, match="already exists"):
            files.copy_entry(host_root, "big.bin", root, "docs")
        root.rename_entry("docs/big.bin", "renamed.bin")
        assert root.delete_entry("docs").removed == 3
    finally:
        root.close()
