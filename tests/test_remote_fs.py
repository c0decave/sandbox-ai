"""Remote folders (sshfs://) as workspaces and mounts: mounted on the host by sandbox-ai, given to the sandbox as a
folder of its own. End to end through ./sandbox-ai with Podman, sshfs and fusermount3 faked (live_fakes.py): real
FUSE is not there. User's decision 2026-11-16: "filesystem support z.B. via sshfs ... fuer das Board und
mount/attach Funktionen, sodass wir z.B. zur Laufzeit auch remote dirs einbinden koennen"."""
import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from live_fakes import Host, ROOT


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def host(tmp_path):
    made = Host(tmp_path)
    made.add_image("localhost/claude-sandbox:latest")
    made.add_sshfs()
    return made


def remote_root(host):
    return host.home / ".local/state/sandbox-ai/remote"


def binds(host):
    """{dst: (src, options)} of every bind mount of the sandbox's podman run."""
    found = {}
    for call in host.calls():
        if call[:1] == ["run"] or "run" in call[:3]:
            for index, arg in enumerate(call):
                if arg == "--mount" and call[index + 1].startswith("type=bind,"):
                    fields = dict(part.partition("=")[::2] for part in call[index + 1].split(","))
                    found[fields.get("dst")] = (fields.get("src"), call[index + 1])
    return found


def sshfs_args(host):
    return [call["args"] for call in host.sshfs_calls() if call["tool"] == "sshfs"]


def unmounts(host):
    return [call["args"] for call in host.sshfs_calls() if call["tool"] == "fusermount3"]


def option_values(args):
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-o"]


def test_dry_run_plans_the_remote_workspace_and_mounts_nothing(host):
    done = host.run("claude", "--dry-run", "--workspace", "sshfs://dev@build1/srv/project")
    assert done.returncode == 0, done.stderr
    plan = json.loads(done.stdout)
    assert [(e["source"], e["mode"], e["sandbox"]) for e in plan["remote"]] == \
        [("sshfs://dev@build1/srv/project", "rw", "/work")]
    assert plan["workspaces"][0]["sandbox"] == "/work"
    assert plan["workspaces"][0]["host"] == plan["remote"][0]["point"]
    assert Path(plan["remote"][0]["point"]).name == "project"
    assert host.sshfs_calls() == []
    # the planned mount point is not left behind
    assert not Path(plan["remote"][0]["point"]).exists()
    assert not any(remote_root(host).iterdir()) if remote_root(host).exists() else True


def test_a_start_mounts_the_remote_workspace_gives_it_as_work_and_unmounts_it_at_the_end(host):
    done = host.run("claude", "--no-tmux", "--workspace", "sshfs://dev@build1/srv/project",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    [args] = sshfs_args(host)
    assert args[0] == "dev@build1:/srv/project"
    point = Path(args[1])
    assert point.is_relative_to(remote_root(host).resolve()) and point.name == "project"
    options = option_values(args)
    assert {"idmap=user", "reconnect", "BatchMode=yes", "StrictHostKeyChecking=yes"} <= set(options)
    assert "ro" not in options and not any("StrictHostKeyChecking=no" in o for o in options)
    src, _ = binds(host)["/work"]
    assert src == str(point)
    assert "changes and deletes files on dev@build1" in done.stderr
    assert "not through the sandbox's proxy" in done.stderr
    # unmounted after the pod, nothing left
    assert unmounts(host) == [["-u", str(point)]]
    assert host.fuse_mounts() == []
    assert not point.exists()
    assert not any(remote_root(host).iterdir())


def test_a_remote_mount_read_only_in_the_remote_home(host):
    done = host.run("claude", "--no-tmux", "--mount", "sshfs://build1:2222/~/data:/mnt/data:ro",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    [args] = sshfs_args(host)
    assert args[0] == "build1:data" and args[args.index("-p") + 1] == "2222"
    assert "ro" in option_values(args)
    src, spec = binds(host)["/mnt/data"]
    assert src == args[1] and ",ro," in spec + ","
    assert "reads the files of that machine (read-only)" in done.stderr
    assert host.fuse_mounts() == []


def test_workspace_ro_mounts_the_remote_folder_read_only(host):
    done = host.run("claude", "--no-tmux", "--workspace-ro", "--workspace", "sshfs://build1/srv/x",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    assert "ro" in option_values(sshfs_args(host)[0])


def test_without_sshfs_the_start_says_how_to_install_it(host):
    (host.bin / "sshfs").unlink()
    done = host.run("claude", "--no-tmux", "--workspace", "sshfs://build1/srv/x", env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode != 0
    assert "needs sshfs on this host" in done.stderr and "apt install sshfs" in done.stderr
    assert "Traceback" not in done.stderr
    assert host.stopped_at() is None and not binds(host)
    assert not any(remote_root(host).iterdir())


def test_a_failed_login_says_what_to_do_and_starts_nothing(host):
    done = host.run("claude", "--no-tmux", "--workspace", "sshfs://build1/srv/x",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_SSHFS_FAIL": "1:dev@build1: Permission denied (publickey)."})
    assert done.returncode != 0
    assert "Permission denied" in done.stderr and "ssh-add" in done.stderr
    assert "Traceback" not in done.stderr
    assert not binds(host)
    assert not any(remote_root(host).iterdir())


@pytest.mark.parametrize("value", ["sshfs://", "sshfs://h:99999/x", "sshfs://u@@h/x", "sshfs://h/~/../etc"])
def test_a_bad_source_is_refused_before_anything_is_made(host, value):
    done = host.run("claude", "--dry-run", "--workspace", value)
    assert done.returncode != 0 and "Traceback" not in done.stderr, done.stderr
    assert host.sshfs_calls() == []


def test_a_host_of_sandbox_ai_hosts_is_reached_as_hosts_reaches_it(host):
    assert host.run("hosts", "add", "build1", "dev@ct1", "--port", "2222").returncode == 0
    done = host.run("claude", "--no-tmux", "--workspace", "sshfs://build1/srv/x", env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    [args] = sshfs_args(host)
    assert args[0] == "dev@ct1:/srv/x" and args[args.index("-p") + 1] == "2222"
    options = option_values(args)
    assert "StrictHostKeyChecking=accept-new" in options
    assert any(o.startswith("UserKnownHostsFile=") and "sandbox-ai/known_hosts" in o for o in options)


def test_a_kept_start_keeps_the_source_and_mounts_it_again_on_resume(host):
    done = host.run("claude", "--name", "s1", "--keep", "--workspace", "sshfs://dev@build1/srv/project",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    record = json.loads(next((host.home / ".local/state/sandbox-ai/sessions").glob("*/session.json")).read_text())
    assert "sshfs://dev@build1/srv/project" in record["options"]
    assert record["workspace"] == "sshfs://dev@build1/srv/project"
    assert len(sshfs_args(host)) == 1 and host.fuse_mounts() == []
    # (the fake sandbox ends at once: resume says so; what counts is that its start mounted the folder again)
    again = host.run("resume", "s1", env={"FAKE_PODMAN_RUN": "0:0"})
    assert "Traceback" not in again.stderr and "mounting sshfs://dev@build1/srv/project" in again.stderr
    assert [args[0] for args in sshfs_args(host)] == ["dev@build1:/srv/project"] * 2
    deadline = time.monotonic() + 30  # its supervisor unmounts it as it ends
    while time.monotonic() < deadline and (host.fuse_mounts() or any(remote_root(host).iterdir())):
        time.sleep(0.1)
    assert host.fuse_mounts() == [] and not any(remote_root(host).iterdir())


def test_gc_unmounts_what_a_killed_launcher_left_mounted(host):
    remote = load("remotefs_gc_test", "sandbox/remotefs.py")
    env = {"XDG_STATE_HOME": str(host.home / ".local/state")}
    old = {key: os.environ.get(key) for key in env}
    os.environ.update(env)
    try:
        entry = remote.plan("claude-deadbeef", "sshfs://build1/srv/x", "rw")
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    state = remote_root(host) / "claude-deadbeef" / "mounts.json"
    data = json.loads(state.read_text())
    data["pid"], data["mounts"][0]["state"] = 999999999, "mounted"
    state.write_text(json.dumps(data))
    Path(host.env["FAKE_FUSE_MOUNTS"]).write_text(json.dumps([entry["point"]]))
    (Path(entry["point"]) / "REMOTE.txt").write_text("remote content\n")
    dry = host.run("gc", "--dry-run")
    assert dry.returncode == 0 and "would unmount the remote folder sshfs://build1/srv/x" in dry.stdout, dry.stdout
    assert unmounts(host) == []
    done = host.run("gc")
    assert done.returncode == 0, done.stderr
    assert "unmounted the remote folder sshfs://build1/srv/x of the ended run claude-deadbeef" in done.stdout
    assert unmounts(host) == [["-u", entry["point"]]]
    assert not (remote_root(host) / "claude-deadbeef").exists()


def test_doctor_names_sshfs(host):
    done = host.run("doctor")
    assert "Remote folders (sshfs)" in done.stdout + done.stderr
    (host.bin / "sshfs").unlink()
    done = host.run("doctor")
    assert "sshfs is not installed" in done.stdout + done.stderr


def test_a_sandbox_that_outlives_its_launcher_keeps_its_remote_folder_until_it_is_stopped(host):
    """Podman could not remove the pod (a full disk): the sandbox runs on without its supervisor, its agent still
    works in the remote folder - so it stays mounted; `sandbox-ai stop` ends the run and unmounts it."""
    no_space = "Error: removing pod: writing to database: no space left on device"
    done = host.run("claude", "--workspace", "sshfs://dev@build1/srv/project",
                    env={"FAKE_PODMAN_RUN": "1:0", "FAKE_PODMAN_POD_RM_FAIL": f"125:{no_space}"})
    assert "runs on without its supervisor" in done.stderr, done.stderr
    [args] = sshfs_args(host)
    assert host.fuse_mounts() == [args[1]] and unmounts(host) == []
    base = Path(host.env["XDG_RUNTIME_DIR"]) / "sandbox-ai" / "runs"
    [kept] = [path for path in base.iterdir() if (path / "run.json").exists()]
    record = json.loads((kept / "run.json").read_text())
    assert record["remote"][0]["source"] == "sshfs://dev@build1/srv/project"
    stopped = host.run("stop", record["name"], env={"FAKE_PODMAN_POD_LIVE": "1"})
    assert stopped.returncode == 0, stopped.stderr
    assert unmounts(host) == [["-u", args[1]]] and host.fuse_mounts() == []
    assert not any(remote_root(host).iterdir())


def test_workspace_add_keeps_a_remote_folder_for_a_stopped_kept_start_and_remove_takes_it_out(host):
    done = host.run("claude", "--name", "s1", "--keep", env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    added = host.run("workspace", "add", "s1", "sshfs://dev@build1/srv/extra/")
    assert added.returncode == 0, added.stderr
    assert "kept for its next start" in added.stdout and host.sshfs_calls() == []  # nothing mounted now
    record = json.loads(next((host.home / ".local/state/sandbox-ai/sessions").glob("*/session.json")).read_text())
    assert record["options"][-2:] == ["--workspace", "sshfs://dev@build1/srv/extra"]
    again = host.run("workspace", "add", "s1", "sshfs://dev@build1/srv/extra")
    assert again.returncode != 0 and "already" in again.stderr
    removed = host.run("workspace", "remove", "s1", "sshfs://dev@build1/srv/extra")
    assert removed.returncode == 0, removed.stderr
    record = json.loads(next((host.home / ".local/state/sandbox-ai/sessions").glob("*/session.json")).read_text())
    assert "sshfs://dev@build1/srv/extra" not in record["options"]


# --- workspace add during the run (the namespace helper faked, as in test_workspaces.py) ---------------------------

W = load("sandbox_workspaces_remote_tests", "sandbox/workspaces.py")
L = W.module("agent_sandbox")


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    run_dir, logs, start = tmp_path / "run", tmp_path / "logs", tmp_path / "start"
    for directory in (run_dir, logs, start):
        directory.mkdir()
    record = {"run": "claude-0badf00d", "backend": "podman", "directory": str(run_dir), "log_dir": str(logs),
              "workspace": str(start),
              "workspace_checks": {"workspaces": [{"host": str(start), "sandbox": "/work", "mode": "rw"}],
                                   "read_only": False, "trees": [], "held": [], "private": [], "stores": []}}
    monkeypatch.setattr(L, "load_policy", lambda path=None: {})
    monkeypatch.setattr(L, "exposed_before", lambda: [])
    monkeypatch.setattr(W, "watch_file_log", lambda record, path, out=print: False)
    monkeypatch.setattr(W, "copies", lambda found, path, proc=None, group=None: [])
    return record


def found(record):
    return {"pids": [100], "mntns": {"a": 100}, "user": 100, "cgroup": "/x"}


def test_workspace_add_mounts_a_remote_folder_on_the_host_and_remove_unmounts_it(running, capsys):
    record = running
    mounted, unmounted, helper, lines = [], [], [], []

    def mounter(entry, interactive):
        mounted.append((entry["source"], entry["point"], entry["mode"], interactive))
        (Path(entry["point"]) / "REMOTE.txt").write_text("x")

    def unmounter(run, points):
        unmounted.append((run, points))
        for point in points:
            (Path(point) / "REMOTE.txt").unlink(missing_ok=True)
            os.rmdir(point)
        return [(None, point, None) for point in points]
    sandbox = W.add_remote(record, "sshfs://dev@build1/srv/data", out=lines.append, interactive=False,
                           mounter=mounter, runner=lambda op, f, name, *rest, **kw: helper.append((op, name, *rest))
                           or (0, ""), finder=found, remember=lambda trees: None)
    assert sandbox == "/workspaces/data"
    [(source, point, mode, interactive)] = mounted
    assert source == "sshfs://dev@build1/srv/data" and mode == "rw" and interactive is False
    assert helper[0][:3] == ("add", "data", point)  # the namespace helper gets the mount point, as any folder
    warned = capsys.readouterr().err
    assert "lies in" not in warned and "sandbox-ai's own" not in warned  # (no false own-folder warning)
    assert any("changes and deletes files on dev@build1" in line for line in lines)
    [row] = [entry for entry in W.listing(record) if entry["source"] == "added"]
    assert row["remote"] == "sshfs://dev@build1/srv/data" and row["host"] == point
    assert "sshfs://dev@build1/srv/data" in "\n".join(W.list_lines(record, W.listing(record)))
    with pytest.raises(ValueError, match="added already"):
        W.add_remote(record, "sshfs://dev@build1/srv/data/", mounter=mounter, finder=found)
    W.remove(record, "sshfs://dev@build1/srv/data", out=lines.append, runner=lambda *a, **k: (0, ""), finder=found,
             using=lambda f, p: [], unmounter=unmounter)
    assert unmounted == [("claude-0badf00d", [point])] and not Path(point).exists()
    assert "removed /workspaces/data (sshfs://dev@build1/srv/data)" in lines[-1]


def test_a_failed_remote_add_leaves_nothing_mounted_or_planned(running):
    record = running
    remote = W.module("remotefs")

    def mounter(entry, interactive):
        raise ValueError("could not mount sshfs://build1/srv/x (sshfs: Permission denied): the login failed")
    with pytest.raises(ValueError, match="Permission denied"):
        W.add_remote(record, "sshfs://build1/srv/x", mounter=mounter, interactive=False, finder=found,
                     unmounter=lambda run, points: remote.release(run, only=points, runner=lambda *a, **k:
                                                                   __import__("subprocess").CompletedProcess(a, 0),
                                                                   check=lambda p: False))
    assert not (remote.root() / "claude-0badf00d").exists()
    assert W.listing(record)[1:] == []


# --- NFS and SMB: the kernel's clients through sudo (faked), fuse-nfs -----------------------------------------------


def sudo_calls(host):
    return [call for call in host.sshfs_calls() if call["tool"] == "sudo"]


def test_an_nfs_export_without_fuse_nfs_is_mounted_through_sudo_and_unmounted_so(host):
    host.add_sudo()
    done = host.run("claude", "--no-tmux", "--mount", "nfs://nas:2049/export/data?vers=4.2:/mnt/data:ro",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    [mount, umount] = [call["args"] for call in sudo_calls(host)]
    point = mount[-1]
    assert mount[:6] == ["-n", "mount", "-t", "nfs", "-o", "nosuid,nodev,ro,soft,timeo=150,retrans=3,port=2049,"
                                                           "vers=4.2"], mount
    assert mount[6] == "nas:/export/data" and Path(point).name == "data"
    assert binds(host)["/mnt/data"][0] == point
    assert umount == ["-n", "umount", point], "unmounted the way it was mounted"
    assert "with the kernel's client through sudo" in done.stderr and "over NFS" in done.stderr
    assert host.fuse_mounts() == [] and not any(remote_root(host).iterdir())


def test_nfs_is_mounted_soft_unless_asked_for_hard(host):
    """Backlog A (review: NFS hard mounts can hang the launcher and the app): the kernel's client mounts soft - a
    server that is gone makes a call fail after timeo x retrans (15 s, 3 times) instead of waiting for ever; a source
    that wants a hard mount says ?hard=1 (documented), and nothing else is taken."""
    remote = W.module("remotefs")
    spec = remote.parse("nfs://nas/export/data")
    command = remote.kernel_command(spec, "/run/p", False, False)
    assert command[command.index("-o") + 1] == "nosuid,nodev,soft,timeo=150,retrans=3"
    hard = remote.kernel_command(remote.parse("nfs://nas/export/data?hard=1"), "/run/p", True, False)
    assert hard[hard.index("-o") + 1] == "nosuid,nodev,ro,hard"
    for bad in ("nfs://nas/export?hard=yes", "nfs://nas/export?soft=1", "nfs://nas/export?hard=1&hard=1"):
        with pytest.raises(ValueError):
            remote.parse(bad)
    smb = remote.kernel_command(remote.parse("smb://nas/share"), "/run/p", False, False)
    assert "soft" in smb[-1].split(","), "SMB (cifs) is soft by default already: said explicitly"
    host.add_sudo()
    done = host.run("claude", "--no-tmux", "--mount", "nfs://nas/export/data?hard=1:/mnt/data:rw",
                    env={"FAKE_PODMAN_RUN": "0:0"})
    assert done.returncode == 0, done.stderr
    mount = sudo_calls(host)[0]["args"]
    assert mount[mount.index("-o") + 1] == "nosuid,nodev,hard", mount


def test_an_smb_share_gets_its_password_from_a_secret_in_a_file_that_is_removed_at_once(host):
    host.add_sudo()
    secret = subprocess.run(host.command("secrets", "set", "NAS_PW"), input="s3cret pw\n", text=True,
                            capture_output=True, env={**host.env, "SANDBOX_AI_SECRETS": "file"}, cwd=host.workspace)
    assert secret.returncode == 0, secret.stderr
    done = host.run("claude", "--no-tmux", "--workspace", "smb://CORP;alice@nas/share/docs?secret=NAS_PW",
                    env={"FAKE_PODMAN_RUN": "0:0", "SANDBOX_AI_SECRETS": "file"})
    assert done.returncode == 0, done.stderr
    [mount, _] = sudo_calls(host)
    args = mount["args"]
    assert args[:5] == ["-n", "mount", "-t", "cifs", "//nas/share/docs"]
    options = args[args.index("-o") + 1].split(",")
    assert {"nosuid", "nodev", f"uid={os.getuid()}", f"gid={os.getgid()}"} <= set(options) and "ro" not in options
    [credentials] = [o.split("=", 1)[1] for o in options if o.startswith("credentials=")]
    assert mount["credentials"] == "username=alice\npassword=s3cret pw\ndomain=CORP\n"
    assert not Path(credentials).exists(), "the password file is gone right after the mount"
    assert "s3cret" not in " ".join(args) and "s3cret" not in done.stderr
    assert binds(host)["/work"][0] == args[5]
    assert host.fuse_mounts() == []


def test_without_a_password_for_sudo_the_start_says_what_to_do(host):
    host.add_sudo()
    done = host.run("claude", "--no-tmux", "--workspace", "smb://nas/public",
                    env={"FAKE_PODMAN_RUN": "0:0", "FAKE_SUDO_FAIL": "1:sudo: a password is required"})
    assert done.returncode != 0 and "Traceback" not in done.stderr
    assert "start in a terminal" in done.stderr and "sudoers" in done.stderr
    assert "guest" in sudo_calls(host)[0]["args"][-1]
    assert not binds(host) and not any(remote_root(host).iterdir())


def test_an_smb_secret_that_is_not_set_is_named(host):
    host.add_sudo()
    done = host.run("claude", "--no-tmux", "--workspace", "smb://alice@nas/share?secret=MISSING",
                    env={"FAKE_PODMAN_RUN": "0:0", "SANDBOX_AI_SECRETS": "file"})
    assert done.returncode != 0 and "the secret MISSING is not set (sandbox-ai secrets set MISSING)" in done.stderr
    assert sudo_calls(host) == []


def test_fuse_nfs_is_started_and_waited_for_until_the_folder_is_mounted(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    remote = load("remotefs_fuse_nfs_test", "sandbox/remotefs.py")
    entry = remote.plan("claude-0a0b0c0d", "nfs://nas/export/data?mount=fuse", "rw")
    started = []

    class Process:
        returncode = None

        def poll(self):
            return None

        def kill(self):
            started.append("killed")
    which = lambda name: f"/usr/bin/{name}"  # noqa: E731
    looks = iter([False, False, True])
    remote.mount(entry, starter=lambda cmd, **kw: started.append(cmd) or Process(), which=which,
                 check=lambda point: next(looks))
    assert started == [["/usr/bin/fuse-nfs", "-n", "nfs://nas/export/data", "-m", entry["point"]]]
    assert entry["via"] == "fuse-nfs"
    failed = remote.plan("claude-0a0b0c0d", "nfs://nas/export/other?mount=fuse", "rw")

    class Ended(Process):
        returncode = 1

        def poll(self):
            return 1

    def failing(cmd, stdout=None, **kw):
        stdout.write("Failed to mount nfs share : mount_cb: RPC error: Mount failed with error MNT3ERR_ACCES(13)\n")
        stdout.flush()
        return Ended()
    with pytest.raises(ValueError, match="MNT3ERR_ACCES.*export allows this host"):
        remote.mount(failed, starter=failing, which=which, check=lambda point: False)
    with pytest.raises(ValueError, match="fuse-nfs on this host"):
        remote.mount(failed, which=lambda name: None)


@pytest.mark.parametrize("value,problem", [("nfs://u@nas/x", "names no user"), ("nfs://nas", "name the export"),
                                           ("smb://nas/", "name the share"), ("nfs://nas/x?mount=bad", "not one of"),
                                           ("smb://nas/s?secret=1x", "not one of"),
                                           ("smb://bad domain;u@nas/s", "write smb://")])
def test_nfs_and_smb_sources_are_checked(value, problem):
    remote = load("remotefs_parse_test", "sandbox/remotefs.py")
    with pytest.raises(ValueError, match=problem):
        remote.parse(value)
