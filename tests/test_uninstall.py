"""sandbox-ai uninstall: the installation this command runs from goes as install.sh --uninstall removes it (install.sh
lists what that is: --uninstall --dry-run), --purge also sandbox-ai's own data and Podman objects, --secrets also the
secrets and logins. A table of what goes first, then a typed y/N (--yes for scripts, refused without a terminal).

End to end through ./sandbox-ai (a checkout: says what to do, removes nothing) and through an installed copy made by
install.sh --from . in a temporary HOME, with the fake Podman of live_fakes.py and a fake secret-tool on PATH - never
this machine's HOME, state, keyring or Podman. Regression tests: nothing outside sandbox-ai's folders is removed
(workspaces, mounted folders, links out of them, other software's images), the secrets survive without --secrets,
refused without a terminal, running starts refused unless --stop-running."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402
import live_fakes  # noqa: E402

home_guard.ensure()

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"sandbox_ai_{name}_for_uninstall_test", ROOT / "sandbox" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


u = load("uninstall")


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


# --- a fake secret-tool (the Secret Service) ---------------------------------------------------------------------

SECRET_TOOL = r'''#!PYTHON
import json, os, re, sys
store = os.environ["FAKE_SECRET_TOOL_STORE"]
try:
    items = json.load(open(store))
except (OSError, ValueError):
    items = []
args = sys.argv[1:]
def attrs(words):
    return dict(zip(words[::2], words[1::2]))
if args[:1] == ["search"]:
    words = [a for a in args[1:] if a != "--all"]
    want = attrs(words)
    found = [i for i in items if all(i.get(k) == v for k, v in want.items())]
    for item in found:
        print("[/org/freedesktop/secrets/collection/login/1]")
        for key, value in item.items():
            print(f"attribute.{key} = {value}")
    sys.exit(0 if found else 1)
if args[:1] == ["clear"]:
    want = attrs(args[1:])
    items = [i for i in items if not all(i.get(k) == v for k, v in want.items())]
    json.dump(items, open(store, "w"))
    sys.exit(0)
if args[:1] == ["lookup"]:
    sys.exit(1)
sys.exit(2)
'''


class World(live_fakes.Host):
    """A user with the fakes on PATH (Podman, a Secret Service), sandbox-ai's folders filled, and things of theirs
    beside them that must survive every uninstall."""

    def __init__(self, directory):
        super().__init__(directory)
        tool = self.bin / "secret-tool"
        tool.write_text(SECRET_TOOL.replace("PYTHON", sys.executable, 1))
        tool.chmod(0o755)
        self.keyring = self.directory / "keyring.json"
        self.keyring.write_text(json.dumps([{"service": "sandbox-ai", "name": "GITEA_TOKEN"},
                                            {"service": "other-app", "name": "THEIRS"}]))
        self.images_file = self.directory / "images.json"
        self.images_file.write_text(json.dumps([
            {"Id": "a" * 64, "Names": ["localhost/claude-sandbox:latest"], "Labels": {"sandbox-ai.agent-version": "2"},
             "Size": 1000, "Containers": 0},
            {"Id": "b" * 64, "Names": ["localhost/sandbox-ai-vpn:latest"], "Labels": {"sandbox-ai.role": "vpn"},
             "Size": 500, "Containers": 0},
            {"Id": "c" * 64, "Names": ["docker.io/library/debian:bookworm"], "Labels": {}, "Size": 700,
             "Containers": 0},
            {"Id": "d" * 64, "Names": ["localhost/mine:latest"], "Labels": {"org.example": "x"}, "Size": 300,
             "Containers": 0}]))
        self.env.update(FAKE_SECRET_TOOL_STORE=str(self.keyring), DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent",
                        FAKE_PODMAN_IMAGES=str(self.images_file), SANDBOX_AI_SECRETS="")
        self.env.pop("SANDBOX_AI_RUN", None)
        self.store.mkdir(parents=True, exist_ok=True)
        (self.store / "fake-volumes.json").write_text(json.dumps([
            {"Name": "pg-data", "Labels": {"sandbox-ai.service-volume": "k"}}, "sandbox-ai-cairn-run1",
            {"Name": "their-volume", "Labels": {}}]))
        self.state = self.home / ".local/state/sandbox-ai"
        self.data = self.home / ".local/share/sandbox-ai"
        self.config = self.home / ".config/sandbox-ai"
        self.cache = self.home / ".cache/sandbox-ai"
        self.outside = self.directory / "project"  # a workspace of theirs, used by a start
        files = {
            self.state / "sessions/web/session.json": json.dumps({"agent": "claude", "workspace": str(self.outside)}),
            self.state / "sessions/web/home/.bashrc": "agent",
            self.state / "logs/proj-1/activity.jsonl": "{}\n",
            self.state / "launcher.log": "x\n",
            self.data / "kits/mine/kit.json": "{}",
            self.data / "mcp-rootfs/abc/README": "x",
            self.data / "web-vendor/xterm.js": "x",
            self.data / "secrets/GITEA_TOKEN": "token",
            self.data / "auth/claude/.claude/.credentials.json": "{}",
            self.data / "vpn/home/state": "key",
            self.data / "app/login-browser/Cookies": "c",
            self.data / "app/other.json": "{}",
            self.config / "defaults.json": "{}",
            self.config / "hosts.json": json.dumps({"version": 1, "hosts": {"lxc1": {"ssh": "user@lxc1"}}}),
            self.config / "git-logins.json": json.dumps({"version": 1, "logins": []}),
            self.config / "publish-logins.json": "{}",
            self.config / "vpn/home.conf": "[Interface]",
            self.config / "web/users.json": "{}",
            self.cache / "mcp-recommended.json": "{}",
            self.outside / "main.py": "print('mine')",
            self.home / ".config/other-tool/config": "theirs",
            self.home / ".local/share/other-tool/data": "theirs",
            self.home / ".local/bin/other-tool": "theirs",
        }
        for path, text in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        # A link in a kept HOME to the workspace: removed as a link, never followed.
        (self.state / "sessions/web/home/project").symlink_to(self.outside)
        (self.data / "mcp-rootfs/abc").chmod(0o555)  # installed root file systems are read-only
        (self.home / ".bashrc").write_text("# mine\nalias ll='ls -l'\n")

    def install(self, *args):
        result = subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--from", str(ROOT), "--no-app-deps",
                                 "--no-app-terminal", *args], capture_output=True, text=True, env=self.env,
                                timeout=300)
        assert result.returncode == 0, result.stderr
        return result

    def installed(self, *args, stdin=subprocess.DEVNULL, input=None):
        """The installed `sandbox-ai ARGS` (no terminal)."""
        kwargs = {"input": input} if input is not None else {"stdin": stdin}
        return subprocess.run([str(self.home / ".local/bin/sandbox-ai"), *args], capture_output=True, text=True,
                              env=self.env, cwd=self.workspace, timeout=300, **kwargs)

    def keyring_names(self, service="sandbox-ai"):
        return sorted(item["name"] for item in json.loads(self.keyring.read_text()) if item["service"] == service)

    def image_names(self):
        return sorted(name for row in json.loads(self.images_file.read_text()) for name in row["Names"])

    def volume_names(self):
        rows = json.loads((self.store / "fake-volumes.json").read_text())
        return sorted(row if isinstance(row, str) else row["Name"] for row in rows)

    def theirs_intact(self):
        assert (self.outside / "main.py").read_text() == "print('mine')", "a workspace was touched"
        assert (self.home / ".config/other-tool/config").read_text() == "theirs"
        assert (self.home / ".local/share/other-tool/data").read_text() == "theirs"
        assert (self.home / ".local/bin/other-tool").read_text() == "theirs"
        assert "alias ll='ls -l'" in (self.home / ".bashrc").read_text()
        assert self.keyring_names("other-app") == ["THEIRS"]
        assert "docker.io/library/debian:bookworm" in self.image_names() and "localhost/mine:latest" in self.image_names()
        assert "their-volume" in self.volume_names()

    def secrets_intact(self):
        assert (self.data / "secrets/GITEA_TOKEN").read_text() == "token"
        assert (self.data / "auth/claude/.claude/.credentials.json").is_file()
        assert (self.data / "vpn/home/state").is_file() and (self.config / "vpn/home.conf").is_file()
        assert (self.data / "app/login-browser/Cookies").is_file()
        assert (self.config / "git-logins.json").is_file() and (self.config / "publish-logins.json").is_file()
        assert self.keyring_names() == ["GITEA_TOKEN"]

    def everything_there(self):
        self.secrets_intact()
        assert (self.state / "sessions/web/home/.bashrc").is_file() and (self.data / "kits/mine/kit.json").is_file()
        assert (self.config / "defaults.json").is_file() and (self.cache / "mcp-recommended.json").is_file()
        assert (self.data / "versions").is_dir() and (self.home / ".local/bin/sandbox-ai").is_symlink()
        assert "localhost/claude-sandbox:latest" in self.image_names() and "pg-data" in self.volume_names()

    def tree(self):
        """Every path below HOME (relative), with a file's content: what an uninstall left."""
        found = {}
        for path in sorted(self.home.rglob("*")):
            relative = str(path.relative_to(self.home))
            if relative.startswith(".local/share/containers"):
                continue
            found[relative] = path.read_bytes().decode(errors="replace") if path.is_file() and not path.is_symlink() else \
                f"-> {os.readlink(path)}" if path.is_symlink() else "dir"
        return found


@pytest.fixture
def world(tmp_path):
    made = World(tmp_path)
    yield made
    for directory, dirs, _ in os.walk(tmp_path):
        for name in dirs:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                os.chmod(child, 0o755)


# --- install.sh lists what --uninstall removes ------------------------------------------------------------------

def test_install_sh_lists_what_uninstall_removes_and_removes_exactly_that(world):
    world.install()
    (world.home / ".config/autostart").mkdir(parents=True)
    (world.home / ".config/autostart/sandbox-ai-app.desktop").write_text(
        f"[Desktop Entry]\nExec={world.data}/current/sandbox-ai-app --hidden\n")
    listed = subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--uninstall", "--dry-run"],
                            capture_output=True, text=True, env=world.env, timeout=60)
    assert listed.returncode == 0, listed.stderr
    rows = [line.split("\t") for line in listed.stdout.splitlines()]
    kinds = {kind for kind, _ in rows}
    assert kinds == {"link", "installation", "menu", "login", "rc", "completion"}, rows
    paths = {path for _, path in rows}
    assert str(world.home / ".local/bin/sandbox-ai") in paths and str(world.data / "versions") in paths
    assert str(world.home / ".bashrc") in paths
    world.everything_there()  # a dry run changes nothing
    before = world.tree()
    result = subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--uninstall"], capture_output=True,
                            text=True, env=world.env, timeout=60)
    assert result.returncode == 0, result.stderr
    after = world.tree()
    for kind, path in rows:
        relative = str(Path(path).relative_to(world.home))
        if kind == "rc":
            assert ">>> sandbox-ai >>>" not in after[relative]
        else:
            assert relative not in after, path
    gone = {path for path in before if path not in after}
    covered = {str(Path(path).relative_to(world.home)) for kind, path in rows if kind != "rc"}
    assert all(any(path == top or path.startswith(top + "/") for top in covered) for path in gone), \
        sorted(path for path in gone if not any(path == t or path.startswith(t + "/") for t in covered))
    world.secrets_intact()
    world.theirs_intact()


def test_a_login_entry_of_another_copy_stays(world):
    world.install()
    entry = world.home / ".config/autostart/sandbox-ai-app.desktop"
    entry.parent.mkdir(parents=True)
    entry.write_text("[Desktop Entry]\nExec=/opt/pip-env/bin/python -I -m sandbox_ai_app --hidden\n")
    listed = subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--uninstall", "--dry-run"],
                            capture_output=True, text=True, env=world.env, timeout=60)
    assert "login\t" not in listed.stdout
    assert subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--uninstall"], capture_output=True,
                          env=world.env, timeout=60).returncode == 0
    assert entry.is_file()


def test_dry_run_belongs_to_uninstall():
    result = subprocess.run(["bash", str(ROOT / "install.sh"), "--dry-run"], capture_output=True, text=True,
                            env={**os.environ, "HOME": "/nonexistent"}, timeout=60)
    assert result.returncode != 0 and "--dry-run goes with --uninstall" in result.stderr


# --- which copy runs ------------------------------------------------------------------------------------------------

def tree_at(path):
    (path / "sandbox").mkdir(parents=True)
    (path / "install.sh").write_text("#!/bin/bash\n")
    return path / "sandbox"


def test_copy_kind(scenario, tmp_path):
    if scenario == "happy":
        here = tree_at(tmp_path / "home/.local/share/sandbox-ai/versions/1.2.3")
        copy = u.copy_kind(here, euid=1000)
        assert copy["kind"] == "user" and copy["data"] == str(tmp_path / "home/.local/share/sandbox-ai")
        assert u.installer_args(copy) == ["--user"]
        prefix = tmp_path / "usr"
        here = tree_at(prefix / "lib/sandbox-ai/versions/1.2.3")
        (prefix / "bin").mkdir()
        (prefix / "bin/sandbox-ai").symlink_to(prefix / "lib/sandbox-ai/current/sandbox-ai")
        copy = u.copy_kind(here, euid=0)
        assert copy["kind"] == "system" and u.installer_args(copy) == ["--system", "--prefix", str(prefix)]
    elif scenario == "sad":
        checkout = tree_at(tmp_path / "src")
        (tmp_path / "src/.git").mkdir()
        copy = u.copy_kind(checkout, euid=1000)
        assert copy["kind"] == "checkout" and "git checkout" in copy["how"] and str(tmp_path / "src") in copy["how"]
        site = tmp_path / "venv/lib/python3.12/site-packages/sandbox_ai"
        site.mkdir(parents=True)
        assert u.copy_kind(site, euid=1000, prefix=str(tmp_path / "venv"))["how"].endswith(
            "pip uninstall sandbox-ai-app")
        pipx = tmp_path / "home/.local/share/pipx/venvs/sandbox-ai-app"
        assert "pipx uninstall sandbox-ai-app" in u.copy_kind(site, euid=1000, prefix=str(pipx))["how"]
        # a system installation, asked by a user who cannot write it: refused, says how
        prefix = tmp_path / "usr"
        here = tree_at(prefix / "lib/sandbox-ai/versions/1.2.3")
        (prefix / "bin").mkdir()
        (prefix / "bin/sandbox-ai").symlink_to(prefix / "lib/sandbox-ai/current/sandbox-ai")
        copy = u.copy_kind(here, euid=1000, writable=lambda path: False)
        assert copy["kind"] == "system" and "sudo sandbox-ai uninstall" in copy["refused"]
    else:
        packaged = tree_at(tmp_path / "squash")
        (tmp_path / "squash/PACKAGING").write_text("appimage\n")
        copy = u.copy_kind(packaged, euid=1000)
        assert copy["kind"] == "package" and "AppImage" in copy["how"]
        loose = tree_at(tmp_path / "unpacked")
        assert u.copy_kind(loose, euid=1000)["kind"] == "folder"


# --- what --purge takes ---------------------------------------------------------------------------------------------

def xdg(tmp_path):
    home = tmp_path / "home"
    env = {"HOME": str(home), "XDG_STATE_HOME": str(home / "state"), "XDG_DATA_HOME": str(home / "data"),
           "XDG_CONFIG_HOME": str(home / "config"), "XDG_CACHE_HOME": str(home / "cache"),
           "XDG_RUNTIME_DIR": str(home / "run")}
    for relative in ("state/sandbox-ai/sessions/a/home/x", "state/sandbox-ai/logs/p/activity.jsonl",
                     "data/sandbox-ai/secrets/TOKEN", "data/sandbox-ai/auth/codex/auth.json",
                     "data/sandbox-ai/vpn/wg/key", "data/sandbox-ai/kits/k/kit.json",
                     "data/sandbox-ai/app/login-browser/Cookies", "data/sandbox-ai/app/videos.json",
                     "data/sandbox-ai/versions/1/sandbox-ai", "data/sandbox-ai/release-state.json",
                     "config/sandbox-ai/git-logins.json", "config/sandbox-ai/publish-logins.json",
                     "config/sandbox-ai/vpn/wg.conf", "config/sandbox-ai/defaults.json", "config/other/keep",
                     "cache/sandbox-ai/x", "cache/sandbox-ai-app/theme/y", "run/sandbox-ai/runs/r/run.json",
                     "data/other/keep"):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    return home, env


def test_purge_targets(scenario, tmp_path):
    home, env = xdg(tmp_path)
    secrets = scenario == "sad"
    if scenario == "edge":  # a folder of sandbox-ai's that is a link elsewhere: left, said
        (home / "elsewhere").mkdir()
        (home / "elsewhere/keep").write_text("x")
        (home / "cache/sandbox-ai/x").unlink()
        (home / "cache/sandbox-ai").rmdir()
        (home / "cache/sandbox-ai").symlink_to(home / "elsewhere")
    found = u.purge_targets(secrets=secrets, env=env, platform="linux")
    paths = {str(Path(target["path"]).relative_to(home)) for target in found["remove"]}
    roots = [str(home / name) for name in ("state/sandbox-ai", "data/sandbox-ai", "config/sandbox-ai",
                                           "cache/sandbox-ai", "cache/sandbox-ai-app", "run/sandbox-ai")]
    for target in found["remove"]:
        assert any(target["path"] == root or target["path"].startswith(root + "/") for root in roots), target
    assert not any(path.startswith(("config/other", "data/other")) for path in paths)
    assert not any("versions" in path or "release-state" in path for path in paths), "install.sh's own"
    kept = {"data/sandbox-ai/secrets", "data/sandbox-ai/auth", "data/sandbox-ai/vpn",
            "data/sandbox-ai/app/login-browser", "config/sandbox-ai/git-logins.json",
            "config/sandbox-ai/publish-logins.json", "config/sandbox-ai/vpn"}
    assert "data/sandbox-ai/app/videos.json" in paths and "state/sandbox-ai/sessions" in paths
    if secrets:
        assert kept <= paths
        assert found["kept"] == []
    else:
        assert not kept & paths and "data/sandbox-ai/app" not in paths
        assert {str(Path(target["path"]).relative_to(home)) for target in found["kept"]} == kept
    if scenario == "edge":
        assert not any(path.startswith("cache/sandbox-ai/") or path == "cache/sandbox-ai" for path in paths)
        assert found["links"] == [str(home / "cache/sandbox-ai")]


def test_macos_places(tmp_path):
    home, env = xdg(tmp_path)
    for relative in ("Library/Caches/sandbox-ai/ssh/sock", "Library/LaunchAgents/org.sandbox-ai.app.plist",
                     "Library/LaunchAgents/com.other.plist"):
        (home / relative).parent.mkdir(parents=True, exist_ok=True)
        (home / relative).write_text("x")
    found = u.purge_targets(secrets=False, env=env, platform="darwin")
    paths = {str(Path(target["path"]).relative_to(home)) for target in found["remove"]}
    assert "Library/Caches/sandbox-ai/ssh" in paths
    assert "Library/LaunchAgents/org.sandbox-ai.app.plist" in paths
    assert "Library/LaunchAgents/com.other.plist" not in paths


# --- removing ---------------------------------------------------------------------------------------------------------

def test_remove_path(scenario, tmp_path):
    root = tmp_path / "state/sandbox-ai"
    outside = tmp_path / "workspace"
    outside.mkdir(parents=True)
    (outside / "work.py").write_text("mine")
    target = root / "sessions/a"
    (target / "home").mkdir(parents=True)
    (target / "home/file").write_text("x")
    (target / "home/link").symlink_to(outside)
    (target / "home/readonly").mkdir()
    (target / "home/readonly/f").write_text("x")
    (target / "home/readonly").chmod(0o555)
    if scenario == "happy":
        assert u.remove_path(target, [root], mounts=set()) is None
        assert not target.exists() and (outside / "work.py").read_text() == "mine"
    elif scenario == "sad":
        mount = target / "home/remote"
        mount.mkdir()
        (mount / "theirs").write_text("remote file")
        why = u.remove_path(target, [root], mounts={str(mount)})
        assert why and "mounted" in why and str(mount) in why
        assert (mount / "theirs").read_text() == "remote file" and (target / "home/file").exists()
        (target / "home/readonly").chmod(0o755)
    else:
        why = u.remove_path(outside, [root], mounts=set())
        assert why and "not in sandbox-ai's folders" in why and (outside / "work.py").exists()
        link = root / "logs"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(outside)
        assert u.remove_path(link, [root], mounts=set()) is None
        assert not link.is_symlink() and (outside / "work.py").exists(), "a link goes as a link"
        (target / "home/readonly").chmod(0o755)


def test_mount_points_read_from_mountinfo(tmp_path):
    info = tmp_path / "mountinfo"
    info.write_text("22 1 0:21 / / rw - ext4 /dev/sda1 rw\n"
                    "40 22 0:50 / /home/u/.local/state/sandbox-ai/remote/my\\040folder rw - fuse.sshfs h:/ rw\n")
    assert u.mount_points(info) == {"/", "/home/u/.local/state/sandbox-ai/remote/my folder"}
    assert u.mount_points(tmp_path / "missing") is None


# --- Podman: sandbox-ai's own only ------------------------------------------------------------------------------------

def test_podman_objects(scenario, tmp_path, monkeypatch):
    rows = {
        ("images",): [{"Id": "a" * 64, "Names": ["localhost/claude-sandbox:latest"],
                       "Labels": {"sandbox-ai.agent-version": "2"}, "Size": 10},
                      {"Id": "c" * 64, "Names": ["docker.io/library/debian:bookworm"], "Labels": None, "Size": 7},
                      {"Id": "e" * 64, "Names": [], "Labels": {"sandbox-ai.role": "vpn"}, "Size": 3}],
        ("volume",): [{"Name": "pg", "Labels": {"sandbox-ai.service-volume": "k"}},
                      {"Name": "sandbox-ai-cairn-r1", "Labels": {}}, {"Name": "theirs", "Labels": {}}],
        ("network",): [{"Name": "podman", "Labels": {}}, {"name": "sbx", "labels": {"sandbox-ai.run": "r"}}],
        ("ps",): [{"Id": "1" * 64, "Names": ["agent-r"], "Labels": {"sandbox-ai.run": "r"}, "State": "exited"},
                  {"Id": "2" * 64, "Names": ["web"], "Labels": {"sandbox-ai.run": "r2"}, "State": "running"},
                  {"Id": "3" * 64, "Names": ["db"], "Labels": {}, "State": "exited"}],
        ("pod",): [{"Name": "agent-r", "Labels": {"sandbox-ai.run": "r"}, "Status": "Exited"}],
    }
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        words = argv[1:]
        if scenario == "sad":
            return subprocess.CompletedProcess(argv, 125, "", "Cannot connect to Podman")
        out = ""
        if words[:1] == ["system"]:
            out = json.dumps({"Images": [{"ImageID": "a" * 12, "UniqueSize": 9}],
                              "Volumes": [{"VolumeName": "pg", "Size": 40}]})
        elif words[:1] in (["images"], ["volume"], ["network"], ["ps"], ["pod"]) and "rm" not in words[:2]:
            out = json.dumps(rows[tuple(words[:1])] if scenario == "happy" else [])
        return subprocess.CompletedProcess(argv, 0, out, "")
    monkeypatch.setattr(u.shutil, "which", lambda name: "/usr/bin/" + name)
    found = u.podman_objects(run)
    if scenario == "sad":
        assert found is None, "no answer: nothing of Podman's is named or removed"
        return
    if scenario == "edge":
        assert found == {"images": [], "volumes": [], "networks": [], "containers": [], "pods": [],
                         "running": []}
        return
    assert [image["ref"] for image in found["images"]] == ["localhost/claude-sandbox:latest", "e" * 12]
    assert found["images"][0]["bytes"] == 9
    assert [volume["name"] for volume in found["volumes"]] == ["pg", "sandbox-ai-cairn-r1"]
    assert found["volumes"][0]["bytes"] == 40
    assert [network["name"] for network in found["networks"]] == ["sbx"]
    assert [container["id"] for container in found["containers"]] == ["1" * 12]
    assert [pod["name"] for pod in found["pods"]] == ["agent-r"]
    assert found["running"] == ["web"]
    calls.clear()
    stayed = u.remove_podman(found, run)
    removed = [argv[1:] for argv in calls if "rm" in argv[1:3]]
    assert removed == [["rm", "1" * 12], ["pod", "rm", "agent-r"], ["image", "rm", "localhost/claude-sandbox:latest"],
                       ["image", "rm", "e" * 12], ["volume", "rm", "pg"], ["volume", "rm", "sandbox-ai-cairn-r1"],
                       ["network", "rm", "sbx"]], "never --force, never another's"
    assert stayed == {}


# --- the command, its questions and refusals (fakes for the slow parts) --------------------------------------------

@pytest.fixture
def fake_copy(tmp_path, monkeypatch):
    """A user installation's plan without install.sh or Podman: what the table and the question are about."""
    home, env = xdg(tmp_path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(u, "copy_kind", lambda *a, **k: {"kind": "user", "data": str(home / "data/sandbox-ai"),
                                                         "prefix": None, "how": None, "refused": None})
    monkeypatch.setattr(u, "installer_targets", lambda copy: [("link", str(home / "bin/sandbox-ai"))])
    ran = []
    monkeypatch.setattr(u, "run_installer", lambda copy: ran.append("installer") or 0)
    monkeypatch.setattr(u, "podman_objects", lambda run=None: None)
    monkeypatch.setattr(u, "keyring_entries", lambda: (None, []))
    monkeypatch.setattr(u, "running_starts", lambda: [])
    monkeypatch.setattr(u, "host_names", lambda: [])
    log = u.launch_log()  # (--purge silences launcher.log for the rest of its process: this one's, given back after)
    monkeypatch.setattr(log, "_write", log._write)
    return home, ran


def test_refused_without_a_terminal_unless_yes(fake_copy, capsys):
    home, ran = fake_copy
    lines = []
    assert u.main(["--purge"], out=lines.append, isatty=lambda: False) == 1
    assert ran == [] and (home / "state/sandbox-ai/sessions").is_dir()
    assert "no terminal" in capsys.readouterr().err and any("Kept starts" in line for line in lines)
    assert u.main(["--purge", "--dry-run"], out=lines.append, isatty=lambda: False) == 0
    assert ran == [] and (home / "state/sandbox-ai/sessions").is_dir()
    assert u.main(["--purge", "--yes"], out=lines.append, isatty=lambda: False) == 0
    assert ran == ["installer"] and not (home / "state/sandbox-ai").exists()
    assert (home / "data/sandbox-ai/secrets/TOKEN").is_file()


def test_the_schedules_timer_is_turned_off_first_and_only_ours(fake_copy, monkeypatch):
    """sandbox-ai schedule enable's systemd user timer would start this copy's main.py every minute after it is gone:
    the plan names it, and it is turned off (systemctl --user disable --now) and its files removed before anything
    else goes - only the files sandbox-ai wrote; the plan says the schedules stay without --purge."""
    home, ran = fake_copy
    sched = u.module("schedule")
    monkeypatch.setattr(sched.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sched.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(u, "TIMER_RUNNER", lambda argv, **kwargs: calls.append((argv, list(ran))) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    units = sched.systemd_dir()
    units.mkdir(parents=True)
    for name, text in sched.systemd_units().items():
        (units / name).write_text(text)
    other = units / "my-own.timer"
    other.write_text("[Timer]\n")
    lines = []
    assert u.main(["--dry-run"], out=lines.append) == 0
    text = "\n".join(lines)
    assert "timer (turned off first)" in text and "sandbox-ai-schedule" in text
    assert "your schedules (sandbox-ai schedule list)" in text
    assert calls == [] and (units / "sandbox-ai-schedule.timer").is_file()
    assert u.main(["--yes"], out=lines.append) == 0
    assert calls[0] == (["systemctl", "--user", "disable", "--now", "sandbox-ai-schedule.timer"], []), \
        "turned off before the installer ran"
    assert calls[-1][0] == ["systemctl", "--user", "daemon-reload"] and ran == ["installer"]
    assert sorted(path.name for path in units.iterdir()) == ["my-own.timer"]
    # someone else's unit of that name: neither named nor touched
    (units / "sandbox-ai-schedule.timer").write_text("[Timer]\nOnCalendar=daily\n")
    calls.clear()
    lines.clear()
    assert u.main(["--yes"], out=lines.append) == 0
    assert calls == [] and (units / "sandbox-ai-schedule.timer").is_file()
    assert "turned off first" not in "\n".join(lines)


def test_the_answer_must_be_typed_yes(fake_copy):
    home, ran = fake_copy
    for answer in ("", "n", "no", "maybe"):
        asked = []
        assert u.main(["--purge", "--secrets"], out=lambda line: None, isatty=lambda: True,
                      ask=lambda text: asked.append(text) or answer) == 1
        assert ran == [] and (home / "data/sandbox-ai/secrets/TOKEN").is_file()
        assert "secrets and logins" in asked[0] and "cannot be recovered" in asked[0]
    assert u.main(["--purge", "--secrets"], out=lambda line: None, isatty=lambda: True, ask=lambda text: "y") == 0
    assert not (home / "data/sandbox-ai/secrets").exists() and ran == ["installer"]


def test_secrets_only_with_purge(fake_copy, capsys):
    with pytest.raises(SystemExit) as exc:
        u.main(["--secrets", "--yes"], out=lambda line: None)
    assert exc.value.code == 2 and "--secrets goes with --purge" in capsys.readouterr().err


def test_running_starts_are_refused_unless_stop_running(fake_copy, monkeypatch, capsys):
    home, ran = fake_copy
    running = [{"name": "web", "kind": "session", "state": "running"},
               {"name": "mcp-fetch", "kind": "mcp", "state": "running"}]
    monkeypatch.setattr(u, "running_starts", lambda: list(running))
    lines = []
    assert u.main(["--purge", "--yes"], out=lines.append) == 1
    text = capsys.readouterr().err
    assert "web (running)" in text and "mcp-fetch (running)" in text and "--stop-running" in text
    assert ran == [] and (home / "state/sandbox-ai/sessions").is_dir()
    stopped = []
    monkeypatch.setattr(u, "stop_running", lambda entries, out: stopped.append([e["name"] for e in entries]) or [])
    assert u.main(["--purge", "--yes", "--stop-running"], out=lines.append) == 0
    assert stopped == [["web", "mcp-fetch"]] and ran == ["installer"] and not (home / "state/sandbox-ai").exists()
    # one that would not stop: nothing removed
    monkeypatch.setattr(u, "stop_running", lambda entries, out: ["web"])
    (home / "state/sandbox-ai/sessions").mkdir(parents=True)
    ran.clear()
    assert u.main(["--purge", "--yes", "--stop-running"], out=lines.append) == 1
    assert ran == [] and (home / "state/sandbox-ai/sessions").is_dir()


def test_a_copy_not_installed_by_install_sh_removes_nothing(fake_copy, monkeypatch, capsys):
    home, ran = fake_copy
    monkeypatch.setattr(u, "copy_kind", lambda *a, **k: {"kind": "pipx", "data": None, "prefix": None,
                                                         "how": "installed with pipx: pipx uninstall sandbox-ai-app",
                                                         "refused": None})
    lines = []
    assert u.main(["--yes"], out=lines.append) == 1, "without --purge: nothing of it is ours to remove"
    assert ran == [] and (home / "state/sandbox-ai/sessions").is_dir() and (home / "data/sandbox-ai/secrets").is_dir()
    said = capsys.readouterr().err
    assert "pipx uninstall sandbox-ai-app" in said and "--purge" in said and lines == []
    monkeypatch.setattr(u, "copy_kind", lambda *a, **k: {"kind": "checkout", "data": None, "prefix": None,
                                                         "how": "a git checkout", "refused": None})
    assert u.main(["--purge", "--secrets", "--yes"], out=lines.append) == 1, "a checkout: never purged from there"
    assert (home / "state/sandbox-ai/sessions").is_dir() and (home / "data/sandbox-ai/secrets").is_dir()


@pytest.mark.parametrize("kind", ["pip", "pipx", "package"])
def test_purge_of_a_pip_or_package_copy_removes_the_data_and_says_how_to_remove_the_program(fake_copy, monkeypatch,
                                                                                            capsys, kind):
    """--purge for a pip, pipx, AppImage or .pyz copy: the data goes as for an install.sh installation (the same
    table, question and rules); the program itself is never removed, its exact removal is said."""
    home, ran = fake_copy
    how = {"pip": "installed with pip: /venv/bin/python -m pip uninstall sandbox-ai-app",
           "pipx": "installed with pipx: pipx uninstall sandbox-ai-app",
           "package": "runs from /apps/sandbox-ai.pyz: delete that file to uninstall it: rm /apps/sandbox-ai.pyz"}[kind]
    monkeypatch.setattr(u, "copy_kind", lambda *a, **k: {"kind": kind, "data": None, "prefix": None, "how": how,
                                                         "refused": None})
    monkeypatch.setattr(u, "installer_targets", lambda copy: pytest.fail("no install.sh installation to list"))
    monkeypatch.setattr(u, "run_installer", lambda copy: pytest.fail("no install.sh installation to remove"))
    lines = []
    assert u.main(["--purge", "--dry-run"], out=lines.append) == 0
    table = "\n".join(lines)
    assert "Kept starts" in table and "The installation" not in table and "Command links" not in table
    assert how in " ".join(table.split()), "the program stays: how to remove it, in the table's Stays"
    assert (home / "state/sandbox-ai/sessions").is_dir()
    asked = []
    lines.clear()
    assert u.main(["--purge"], out=lines.append, isatty=lambda: True, ask=lambda text: asked.append(text) or "y") == 0
    assert "the program stays" in asked[0]
    assert not (home / "state/sandbox-ai").exists() and not (home / "config/sandbox-ai/defaults.json").exists()
    assert (home / "data/sandbox-ai/secrets/TOKEN").is_file(), "the secrets only with --secrets"
    assert (home / "data/sandbox-ai/versions/1/sandbox-ai").is_file(), "an install.sh installation is not ours here"
    assert (home / "config/other/keep").is_file() and (home / "data/other/keep").is_file()
    assert how in lines[-1] and lines[-1].startswith("the program itself stays")
    assert u.main(["--purge", "--secrets", "--yes"], out=lines.append) == 0
    assert not (home / "data/sandbox-ai/secrets").exists()


def test_copy_kind_names_the_program_to_remove(tmp_path):
    pyz = tmp_path / "apps/sandbox-ai.pyz"
    pyz.parent.mkdir()
    pyz.write_text("zip")
    packaged = tree_at(tmp_path / "cache/sandbox-ai/pyz/abc/tree")
    (packaged.parent / "PACKAGING").write_text("pyz\n")
    link = tmp_path / "bin/sandbox-ai"
    link.parent.mkdir()
    link.symlink_to(pyz)
    copy = u.copy_kind(packaged, euid=1000, environ={"SANDBOX_AI_PACKAGE": str(pyz), "PATH": str(tmp_path / "bin"),
                                                     "HOME": str(tmp_path / "home")})
    assert copy["kind"] == "package" and copy["program"] == str(pyz)
    assert copy["how"].endswith(f"rm {pyz} {link}"), "the file and the link to it, exactly"
    appimage = tmp_path / "apps/My Apps/sandbox-ai-x86_64.AppImage"
    appimage.parent.mkdir()
    appimage.write_text("elf")
    (packaged.parent / "PACKAGING").write_text("appimage\n")
    copy = u.copy_kind(packaged, euid=1000, environ={"SANDBOX_AI_PACKAGE": str(appimage), "PATH": ""})
    assert copy["program"] == str(appimage) and f"rm '{appimage}'" in copy["how"], "quoted for the shell"
    # a package path that is not there (or none at all): the general words, never a path
    copy = u.copy_kind(packaged, euid=1000, environ={"SANDBOX_AI_PACKAGE": str(tmp_path / "gone.AppImage")})
    assert copy["program"] is None and "AppImage" in copy["how"] and "gone" not in copy["how"]
    site = tmp_path / "venv/lib/python3.12/site-packages/sandbox_ai"
    site.mkdir(parents=True)
    (tmp_path / "venv/bin").mkdir()
    (tmp_path / "venv/bin/python").write_text("")
    copy = u.copy_kind(site, euid=1000, prefix=str(tmp_path / "venv"), environ={})
    assert copy["how"].endswith(f"{tmp_path / 'venv/bin/python'} -m pip uninstall sandbox-ai-app")


# --- end to end ---------------------------------------------------------------------------------------------------

def test_e2e_a_checkout_says_what_to_do_and_removes_nothing(world):
    for args in (["uninstall", "--yes"], ["uninstall", "--purge", "--secrets", "--yes"], ["uninstall", "--dry-run"]):
        result = world.run(*args)
        assert result.returncode == 1, (args, result.stdout, result.stderr)
        assert "git checkout" in result.stdout + result.stderr and str(ROOT) in result.stdout + result.stderr
    assert (world.state / "sessions/web/home/.bashrc").is_file() and (world.config / "defaults.json").is_file()
    world.secrets_intact()
    world.theirs_intact()
    assert "localhost/claude-sandbox:latest" in world.image_names()


def test_e2e_installed_copy(world):
    world.install()
    world.everything_there()
    # No terminal and no --yes: refused, nothing removed.
    refused = world.installed("uninstall", "--purge")
    assert refused.returncode == 1 and "--yes" in refused.stdout + refused.stderr
    world.everything_there()
    # A typed answer that is not yes: nothing removed.
    asked = world.installed("uninstall", "--purge", input="n\n")
    assert asked.returncode == 1, asked.stderr
    world.everything_there()
    # The dry run: the table, nothing removed.
    shown = world.installed("uninstall", "--purge", "--dry-run")
    assert shown.returncode == 0, shown.stderr
    table = shown.stdout
    for text in ("Kept starts and their HOMEs", "Logs", "Settings", "Command links", "The installation",
                 "PATH block", "Podman images", "localhost/claude-sandbox:latest", "Podman volumes"):
        assert text in table, (text, table)
    assert "debian" not in table and "their-volume" not in table and "localhost/mine" not in table
    assert "secrets (file store)" in " ".join(table.split()) and "--secrets" in table  # named among what stays
    assert "lxc1" in table, "the hosts of sandbox-ai hosts are named: they keep their own installation"
    world.everything_there()
    # A running start: refused, named; nothing removed.
    # a launcher as net.stop knows one: this user's, started before it registered, running agent_sandbox.py
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "agent_sandbox.py"])
    time.sleep(1.1)
    try:
        runs = world.runtime / "sandbox-ai/runs/claude-1a2b"
        runs.mkdir(parents=True)
        (runs / "run.json").write_text(json.dumps({"run": "claude-1a2b", "agent": "claude", "backend": "podman",
                                                   "workspace": str(world.outside), "pid": sleeper.pid,
                                                   "name": "claude-1a2b",
                                                   "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}))
        busy = world.installed("uninstall", "--purge", "--yes")
        assert busy.returncode == 1 and "claude-1a2b" in busy.stdout + busy.stderr, busy.stdout + busy.stderr
        assert "--stop-running" in busy.stdout + busy.stderr
        world.everything_there()
        # --stop-running: stopped through the normal stop path first, then everything goes but the secrets.
        done = world.installed("uninstall", "--purge", "--yes", "--stop-running")
        assert done.returncode == 0, done.stdout + done.stderr
        assert sleeper.wait(timeout=30) is not None
    finally:
        if sleeper.poll() is None:
            sleeper.send_signal(signal.SIGKILL)
            sleeper.wait()
    assert not world.state.exists() and not world.cache.exists() and not (world.runtime / "sandbox-ai").exists()
    assert not (world.data / "versions").exists() and not (world.data / "kits").exists()
    assert not (world.home / ".local/bin/sandbox-ai").exists() and not (world.home / ".local/bin/sandbox-ai").is_symlink()
    assert ">>> sandbox-ai >>>" not in (world.home / ".bashrc").read_text()
    assert not (world.config / "defaults.json").exists() and not (world.config / "web").exists()
    assert not (world.data / "app/other.json").exists()
    assert world.image_names() == ["docker.io/library/debian:bookworm", "localhost/mine:latest"]
    assert world.volume_names() == ["their-volume"]
    world.secrets_intact()
    world.theirs_intact()
    # Installed again: --secrets takes the secrets and logins too, and the keyring's entries of sandbox-ai.
    world.install()
    gone = world.installed("uninstall", "--purge", "--secrets", "--yes")
    assert gone.returncode == 0, gone.stdout + gone.stderr
    assert not world.data.exists() and not world.config.exists()
    assert world.keyring_names() == []
    world.theirs_intact()


def test_e2e_purge_from_a_pyz_copy(world):
    """End to end: sandbox-ai as the .pyz runs it (its tree extracted into the cache, PACKAGING pyz,
    SANDBOX_AI_PACKAGE naming the .pyz): --purge removes the data, its own extracted copy with it, never the .pyz,
    and says how to remove that."""
    import shutil
    tree = world.cache / "pyz/0123abcd/tree"
    shutil.copytree(ROOT / "sandbox", tree / "sandbox", symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "builtin"))
    (tree / "PACKAGING").write_text("pyz\n")
    pyz = world.home / "Downloads/sandbox-ai.pyz"
    pyz.parent.mkdir(parents=True)
    pyz.write_text("#!/bin/sh\n")

    def run(*args):
        return subprocess.run([sys.executable, "-I", str(tree / "sandbox/main.py"), *args], capture_output=True,
                              text=True, env={**world.env, "SANDBOX_AI_PACKAGE": str(pyz)}, cwd=world.workspace,
                              stdin=subprocess.DEVNULL, timeout=300)
    plain = run("uninstall", "--yes")
    assert plain.returncode == 1 and f"rm {pyz}" in plain.stderr and "--purge" in plain.stderr, plain.stderr
    world.secrets_intact()
    assert (world.state / "sessions/web/home/.bashrc").is_file()
    done = run("uninstall", "--purge", "--yes")
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"rm {pyz}" in done.stdout and "the program itself stays" in done.stdout
    assert pyz.read_text() == "#!/bin/sh\n", "the .pyz is the human's: never removed"
    assert not world.state.exists() and not world.cache.exists() and not (world.config / "defaults.json").exists()
    assert world.image_names() == ["docker.io/library/debian:bookworm", "localhost/mine:latest"]
    world.secrets_intact()
    world.theirs_intact()


def test_e2e_plain_uninstall_is_install_sh_uninstall(tmp_path):
    """sandbox-ai uninstall without --purge leaves exactly what install.sh --uninstall leaves."""
    by_script, by_command = World(tmp_path / "a"), World(tmp_path / "b")
    for world in (by_script, by_command):
        world.install()
    script = subprocess.run(["bash", str(ROOT / "install.sh"), "--user", "--uninstall"], capture_output=True,
                            text=True, env=by_script.env, timeout=60)
    assert script.returncode == 0, script.stderr
    command = by_command.installed("uninstall", "--yes")
    assert command.returncode == 0, command.stdout + command.stderr

    def normal(world):  # (launcher.log: the command logs what it was asked, as every command does)
        return {path: text.replace(str(world.directory), "DIR") if not path.endswith("launcher.log") else "log"
                for path, text in world.tree().items()}
    assert normal(by_script) == normal(by_command)
    by_command.secrets_intact()
    by_command.theirs_intact()
    assert "localhost/claude-sandbox:latest" in by_command.image_names()
    assert (by_command.state / "sessions/web/home/.bashrc").is_file()
    for world in (by_script, by_command):
        for directory, dirs, _ in os.walk(world.directory):
            for name in dirs:
                if not os.path.islink(os.path.join(directory, name)):
                    os.chmod(os.path.join(directory, name), 0o755)


def test_e2e_system_installation(world, tmp_path):
    prefix = tmp_path / "prefix"
    result = subprocess.run(["bash", str(ROOT / "install.sh"), "--system", "--prefix", str(prefix), "--from",
                             str(ROOT), "--no-app-deps", "--no-app-terminal"], capture_output=True, text=True,
                            env=world.env, timeout=300)
    assert result.returncode == 0, result.stderr
    assert (prefix / "bin/sandbox-ai").is_symlink()
    done = subprocess.run([str(prefix / "bin/sandbox-ai"), "uninstall", "--yes"], capture_output=True, text=True,
                          env=world.env, timeout=300, stdin=subprocess.DEVNULL)
    assert done.returncode == 0, done.stdout + done.stderr
    assert not (prefix / "lib/sandbox-ai").exists() and not (prefix / "bin/sandbox-ai").is_symlink()
    assert not (prefix / "share/bash-completion/completions/sandbox-ai").exists()
    assert (world.state / "sessions/web/home/.bashrc").is_file()
    world.secrets_intact()
    world.theirs_intact()


def test_a_start_that_would_not_stop_says_the_timer_is_off_already(fake_copy, monkeypatch, capsys):
    """Review 3x-2: the schedules' timer goes first (no scheduled run may start while the rest goes); when a running
    start then would not stop, the uninstall ends - and says that the timer is off now, and how to turn it on again,
    instead of "nothing removed"."""
    home, ran = fake_copy
    sched = u.module("schedule")
    monkeypatch.setattr(sched.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sched.sys, "platform", "linux")
    monkeypatch.setattr(u, "TIMER_RUNNER", lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, "", ""))
    units = sched.systemd_dir()
    units.mkdir(parents=True)
    for name, text in sched.systemd_units().items():
        (units / name).write_text(text)
    monkeypatch.setattr(u, "running_starts", lambda: [{"name": "web", "kind": "session", "state": "running"}])
    monkeypatch.setattr(u, "stop_running", lambda entries, out: ["web"])
    assert u.main(["--yes", "--stop-running"], out=lambda line: None) == 1
    said = capsys.readouterr().err
    assert "still running: web" in said and "nothing removed" not in said
    assert "the schedules' timer is off now" in said and "sandbox-ai schedule enable" in said
    assert ran == []


def test_the_update_timer_is_turned_off_first_and_only_ours(fake_copy, monkeypatch):
    """sandbox-ai update mode's timer would run this copy's update every 15 minutes after it is gone: named in the
    plan, turned off and its files removed - only those sandbox-ai wrote."""
    home, ran = fake_copy
    updates = u.module("updates")
    sched = updates.load_module("schedule")
    monkeypatch.setattr(sched.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(sched.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(u, "TIMER_RUNNER", lambda argv, **kwargs: calls.append((argv, list(ran))) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    units = sched.systemd_dir()
    units.mkdir(parents=True)
    spec = updates.timer()
    spec.pop("schedule")
    for name, text in sched.systemd_units(timer=spec).items():
        (units / name).write_text(text)
    lines = []
    assert u.main(["--dry-run"], out=lines.append) == 0
    assert "The update timer (turned off first)" in "\n".join(lines) and "sandbox-ai-update" in "\n".join(lines)
    assert calls == [] and (units / "sandbox-ai-update.timer").is_file()
    assert u.main(["--yes"], out=lines.append) == 0
    assert calls[0] == (["systemctl", "--user", "disable", "--now", "sandbox-ai-update.timer"], []), \
        "turned off before the installer ran"
    assert not any(units.iterdir())
