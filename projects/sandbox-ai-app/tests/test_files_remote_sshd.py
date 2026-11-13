"""End to end against a real OpenSSH server: the test starts sshd itself (this user, a free port on 127.0.0.1, a host
key and a user key of its own, key login only, the built-in SFTP), connects as the Files page does - the unknown host
refused, its keys read with ssh-keyscan and trusted, then connected - and browses, copies both ways, edits and deletes
through the page. Skipped, with the reason, where there is no sshd or it cannot start (CI installs openssh-server;
$SANDBOX_AI_TEST_SSHD names another sshd, $SANDBOX_AI_TEST_SSHD_LIBRARY_PATH the folder of its libraries)."""
import getpass
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time

import pytest

from sandbox_ai_app import files, remote


def sshd_binary():
    for path in (os.environ.get("SANDBOX_AI_TEST_SSHD"), "/usr/sbin/sshd", "/usr/local/sbin/sshd",
                 "/opt/homebrew/sbin/sshd"):
        if path and os.access(path, os.X_OK):
            return path
    return None


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def sshd(tmp_path):
    binary, keygen = sshd_binary(), shutil.which("ssh-keygen")
    if binary is None or keygen is None or shutil.which("ssh") is None or shutil.which("ssh-keyscan") is None:
        pytest.skip("no OpenSSH server here (sshd, ssh, ssh-keygen, ssh-keyscan): install openssh-server")
    folder = tmp_path / "sshd"
    folder.mkdir()
    for name in ("host", "user"):
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(folder / name)], check=True)
    (folder / "authorized_keys").write_text((folder / "user.pub").read_text())
    (folder / "authorized_keys").chmod(0o600)
    port = free_port()
    config = folder / "sshd_config"
    config.write_text("\n".join([
        f"Port {port}", "ListenAddress 127.0.0.1", f"HostKey {folder / 'host'}",
        f"AuthorizedKeysFile {folder / 'authorized_keys'}", "PasswordAuthentication no",
        "KbdInteractiveAuthentication no", "UsePAM no", "StrictModes no", f"PidFile {folder / 'sshd.pid'}",
        "Subsystem sftp internal-sftp", "PermitRootLogin prohibit-password", ""]))
    env = dict(os.environ)
    if os.environ.get("SANDBOX_AI_TEST_SSHD_LIBRARY_PATH"):
        env["LD_LIBRARY_PATH"] = os.environ["SANDBOX_AI_TEST_SSHD_LIBRARY_PATH"]
    log = folder / "log"
    process = subprocess.Popen([binary, "-D", "-f", str(config), "-E", str(log)], env=env,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        process.kill()
        pytest.skip("sshd did not listen within 10 s")
    if process.poll() is not None:
        reason = log.read_text(errors="replace").strip().splitlines()[-1:] if log.exists() else ["?"]
        pytest.skip(f"sshd cannot start here: {reason[0] if reason else '?'}")
    host_print = subprocess.run([keygen, "-l", "-f", str(folder / "host.pub")], capture_output=True, text=True).stdout
    yield {"target": remote.Target("127.0.0.1", getpass.getuser(), port, str(folder / "user")),
           "known_hosts": str(tmp_path / "known_hosts"), "fingerprint": host_print.split()[1], "log": log}
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def test_a_real_ssh_server_from_unknown_host_to_copies_both_ways(sshd, tmp_path):
    target, known = sshd["target"], sshd["known_hosts"]
    with pytest.raises(remote.HostKeyUnknown):
        remote.RemoteRoot(target, "/", known_hosts=known, config=os.devnull)
    lines = remote.scan_host(target)
    prints = remote.fingerprints(lines)
    assert any(sshd["fingerprint"] in shown for shown in prints), "the fingerprint shown is the server's"
    remote.add_known_host(lines, known)
    root = remote.RemoteRoot(target, "/", known_hosts=known, config=os.devnull)
    try:
        served = tmp_path / "served"
        (served / "docs").mkdir(parents=True)
        (served / "docs" / "a.txt").write_text("alpha")
        (served / "out").symlink_to("/etc")
        relative = str(served).lstrip("/")
        assert [e.name for e in root.list(relative)] == ["docs", "out"]
        with pytest.raises(files.Refused, match="not followed"):
            root.list(relative + "/out")
        (tmp_path / "host_side").mkdir()
        host = files.Root(tmp_path / "host_side", sandbox=False)
        files.copy_entry(root, relative + "/docs", host, "")
        assert (tmp_path / "host_side" / "docs" / "a.txt").read_text() == "alpha"
        (tmp_path / "host_side" / "up.bin").write_bytes(os.urandom(200_000))
        files.copy_entry(host, "up.bin", root, relative)
        assert (served / "up.bin").read_bytes() == (tmp_path / "host_side" / "up.bin").read_bytes()
        data = root.read_file(relative + "/docs/a.txt")
        root.write_file(relative + "/docs/a.txt", b"over ssh", expected=data.identity)
        assert (served / "docs" / "a.txt").read_text() == "over ssh"
        assert root.free_space(relative)[1] > 0
        assert root.delete_entry(relative + "/docs").removed == 2
    finally:
        root.close()


def test_the_files_page_against_a_real_ssh_server(sshd, tmp_path, wait, monkeypatch):
    pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
    from sandbox_ai_app import files_page, widgets
    from test_files_page import FakeSai, settle
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(widgets, "SHOWN", [])
    ws = tmp_path / "ws"
    ws.mkdir()
    served = tmp_path / "served"
    (served / "docs").mkdir(parents=True)
    (served / "docs" / "notes.md").write_text("# Notes\n")
    page = files_page.FilesPage(FakeSai([("run", "claude-abc", str(ws.resolve()))]))
    page.known_hosts, page.ssh_config = sshd["known_hosts"], os.devnull
    assert wait(lambda: page.roots.count() > 0 and not page.roots_reading)
    page.right.go_path(str(ws))
    assert page.connect_remote(sshd["target"])
    assert wait(lambda: page.remotes and page.left.remote(), 30), page.message.text()
    assert Path(sshd["known_hosts"]).read_text().strip(), "trusted after the (automatic) yes"
    left = page.left
    settle(left)
    left.go_path(str(served / "docs"))
    settle(left, page.right)
    widgets.select_keys(left.table, ["notes.md"])
    assert page.copy_selected(left)
    assert wait(lambda: not page.task.running, 30)
    assert (ws / "notes.md").read_text() == "# Notes\n"
    for root in page.remotes.values():
        root.close()
