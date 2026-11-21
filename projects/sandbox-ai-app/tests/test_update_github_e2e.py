"""Settings › Updates against a fake GitHub, end to end, for every kind of copy the app runs from: install.sh's
installation of a user's own and a system one, the AppImage, the macOS app and a pip installation - Check now, Install
now… (downloaded, checked, installed), the new version, "Restart the app" offered, Roll back…; then the start of the
new app, which takes over from the older one that still runs (the real launchers, offscreen).

The releases are real ones (sandbox-ai's tests/release_kit.py: make_dist.py over a copy of its checkout, signed with a
test key; stand-ins only for what CI alone builds) on sandbox-ai's tests/fake_github.py, which the unchanged updater
code reaches as https://github.com/c0decave/sandbox-ai - the app's buttons run the real `sandbox-ai update` of each
installation."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6.QtNetwork", reason="PySide6 is not installed")

from sandbox_ai_app import backend, instance_probe, updates_view, widgets  # noqa: E402
from test_single_instance_e2e import answers, ended  # noqa: E402

APP_ROOT = Path(__file__).resolve().parents[1]  # projects/sandbox-ai-app
SAI = Path(os.environ.get("SANDBOX_AI_HOME") or APP_ROOT.parents[1])
HAVE_KIT = (SAI / "tests" / "release_kit.py").is_file() and (SAI / "scripts" / "make_dist.py").is_file()
if HAVE_KIT:
    sys.path.insert(0, str(SAI / "tests"))
    import fake_github  # noqa: E402
    import release_kit as kit  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (HAVE_KIT and shutil.which("openssl") and shutil.which("ssh-keygen") and shutil.which("curl")),
    reason="needs sandbox-ai's checkout (SANDBOX_AI_HOME) with its tests/, and openssl, ssh-keygen and curl")

OLD = kit.VERSION if HAVE_KIT else "0"
NEW = kit.later(OLD) if HAVE_KIT else "1"
MACHINE = os.uname().machine
LONG = 600  # seconds an update may take here (a first start unpacks and compiles a package)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Two signed releases of sandbox-ai, OLD (its version) and NEW (the next), every file of each."""
    base = tmp_path_factory.mktemp("releases")
    key = kit.new_key(base / "keys")
    # The python3 of the packages' stand-ins: this Python (it has the app's PySide6).
    python = base / "bin" / "python3"
    python.parent.mkdir()
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    trees, dists = {}, {}
    for version in (OLD, NEW):
        trees[version] = kit.tree_copy(base / f"tree-{version}", version, [key])
        dists[version] = kit.full_release(trees[version], base / f"dist-{version}", key, base / "work")
    yield SimpleNamespace(base=base, key=key, trees=trees, dists=dists, bin=python.parent)
    kit.remove(base)


@pytest.fixture
def github(world):
    fake = fake_github.FakeGitHub(world.base / "tls")
    fake.publish(OLD, world.dists[OLD])
    fake.also = set()
    yield fake
    fake.close()
    assert fake.leaked == [], "a token went to the files' host"
    assert set(fake.refused) <= fake.also, f"something other than GitHub was asked: {fake.refused}"


@pytest.fixture
def machine(tmp_path, world, github, monkeypatch):
    """This process as a user's session on a machine that reaches the fake GitHub: HOME and the XDG folders in
    TMP_PATH, the proxy and the certificate of the fake, python3 (with PySide6), systemctl and hdiutil stand-ins
    first on PATH. What the app's commands inherit."""
    home = tmp_path / "home"
    stubs = kit.hdiutil_stub(tmp_path / "stubs")
    for name in ("systemctl", "launchctl", "notify-send", "osascript"):
        (stubs / name).write_text("#!/bin/sh\nexit 0\n")
        (stubs / name).chmod(0o755)
    runtime = Path(tempfile.mkdtemp(prefix="upd-"))  # (the app's socket: a short path)
    runtime.chmod(0o700)
    env = github.env({})
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), XDG_STATE_HOME=str(home / ".local/state"),
               XDG_DATA_HOME=str(home / ".local/share"), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_RUNTIME_DIR=str(runtime), TMPDIR=str(tmp_path), NO_COLOR="1",
               PATH=f"{stubs}:{world.bin}:{home}/.local/bin:/usr/bin:/bin")
    home.mkdir()
    for name in ("no_proxy", "NO_PROXY", "all_proxy", "ALL_PROXY", "SANDBOX_AI_SOURCE", "SANDBOX_AI_HOME",
                 "SANDBOX_AI_PACKAGE", "SANDBOX_AI_PACKAGE_COPY", "SANDBOX_AI_TOKEN", "APPIMAGE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)  # ask_first: yes
    monkeypatch.setattr(widgets, "SHOWN", [])
    yield SimpleNamespace(home=home, stubs=stubs, runtime=runtime, tmp=tmp_path)
    shutil.rmtree(runtime, ignore_errors=True)
    kit.remove(tmp_path)  # (the installations and the read-only copies the packages unpacked: some 100 MB a test)


def run(argv, seconds=LONG, **kwargs):
    return subprocess.run([str(word) for word in argv], capture_output=True, text=True, timeout=seconds,
                          stdin=subprocess.DEVNULL, **kwargs)


def version_of(command):
    done = run([*command, "--version"])
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class Window:
    """The main window's part of an update: what it is told when one ends ("Restart the app" is offered then)."""
    def __init__(self, offer=None):
        self.ended, self.update_offer, self.terminal = [], offer, []

    def update_ended(self, version, status):
        self.ended.append((version, status))

    def update_now(self):
        self.terminal.append("update now")


def panel_of(tree, qapp, wait, monkeypatch, window=None):
    """Settings › Updates of the app whose sandbox-ai is the tree TREE (its sandbox/ folder inside)."""
    monkeypatch.setenv("SANDBOX_AI_HOME", str(tree))
    sai = backend.SandboxAI()
    assert Path(sai.directory) == Path(tree) / "sandbox" or Path(sai.directory) == Path(tree)
    window = window or Window()
    panel = updates_view.UpdatesPanel(sai, window=window)
    assert wait(lambda: panel.data is not None, 120), panel.state.text()
    return panel, window


def check_install_roll_back(panel, window, wait, world, github, command, kind):
    """The buttons, in the order a human presses them; COMMAND: the installation's own command line (--version)."""
    assert panel.data["kind"] == kind and panel.data["installed"] == OLD
    assert "up to date" in panel.state.text() or "not checked yet" in panel.state.text() or \
        "never" in panel.state.text(), panel.state.text()
    assert not panel.install_button.isEnabled(), "nothing newer is there yet"
    github.publish(NEW, world.dists[NEW])
    panel.check_button.click()
    assert wait(lambda: f"{NEW} is available (installed: {OLD}), signed by a trusted key" in panel.state.text(), 240), \
        panel.state.text() + " / " + panel.status.text()
    assert panel.data["source"] == "https://github.com/c0decave/sandbox-ai"
    assert panel.install_button.isEnabled()
    panel.install_button.click()
    assert wait(lambda: window.ended or panel.status.text() == "Not done.", LONG), panel.status.text()
    assert window.ended == [(NEW, 0)], (panel.status.text(), widgets.SHOWN)
    assert window.terminal == [], "no terminal: downloaded, checked and installed in the background"
    question = next(text for title, text, _ in widgets.SHOWN if "Download sandbox-ai" in text)
    assert f"Download sandbox-ai {NEW} now, check it" in question
    assert f"installed {NEW} (was {OLD})" in panel.status.text()
    assert version_of(command) == f"sandbox-ai {NEW}"
    assert wait(lambda: (panel.data or {}).get("installed") == NEW and panel.data.get("rollback") == OLD, 120), panel.data
    assert f"Last change: {OLD} → {NEW}" in panel.state.text()
    assert not panel.install_button.isEnabled() and panel.rollback_button.isEnabled()
    assert sorted(set(github.downloads)) >= ["SHA256SUMS", "SHA256SUMS.sig"]
    panel.rollback_button.click()
    assert wait(lambda: f"back to {OLD} (was {NEW})" in panel.status.text() or panel.status.text() == "Not done.",
                LONG), panel.status.text()
    assert f"back to {OLD} (was {NEW})" in panel.status.text(), widgets.SHOWN
    assert version_of(command) == f"sandbox-ai {OLD}"
    assert window.ended == [(NEW, 0), (OLD, 0)], "after a rollback the restart is offered too"


APP_OFF = ["--no-app-deps", "--no-app-terminal"]
# By hand, install.sh also looks after the app's own needs (xterm.js for its terminal tabs, PySide6 where python3 has
# none), as `sandbox-ai update` in a terminal does: those hosts are asked, and the fake - only GitHub - refuses them.
BY_HAND = {"pypi.org:443", "registry.npmjs.org:443", "files.pythonhosted.org:443"}


# -- install.sh's installations ---------------------------------------------------------------------------------------

def test_the_app_updates_a_users_installation_from_github(world, github, machine, qapp, wait, monkeypatch):
    first = run(["bash", world.dists[OLD] / "install.sh", "--user", *APP_OFF])
    assert first.returncode == 0, first.stderr
    data = machine.home / ".local/share/sandbox-ai"
    github.also = BY_HAND
    panel, window = panel_of(data / "current", qapp, wait, monkeypatch)
    check_install_roll_back(panel, window, wait, world, github, [machine.home / ".local/bin/sandbox-ai"], "user")
    assert kit.assets.bundle(NEW) in github.downloads


def system_prefix(base):
    """A prefix for a system installation in nobody's home, where programs may run - or None."""
    for folder in (base, "/tmp", "/var/tmp", "/dev/shm"):
        folder = str(folder)
        if not os.access(folder, os.W_OK) or folder.startswith(("/home/", "/root/", "/Users/", "/var/home/")):
            continue
        prefix = Path(tempfile.mkdtemp(prefix="sandbox-ai-system-", dir=folder))
        probe = prefix / "probe"
        probe.write_text("#!/bin/sh\nexit 0\n")
        probe.chmod(0o755)
        try:
            runs = subprocess.run([str(probe)], capture_output=True).returncode == 0
        except OSError:
            runs = False
        probe.unlink()
        if runs:
            return prefix
        shutil.rmtree(prefix, ignore_errors=True)
    return None


@pytest.fixture
def system(world, machine):
    prefix = system_prefix(machine.tmp)
    if prefix is None:
        pytest.skip("no folder outside the homes to install into")
    first = run(["bash", world.dists[OLD] / "install.sh", "--system", "--prefix", prefix, *APP_OFF])
    assert first.returncode == 0, first.stderr
    yield prefix
    subprocess.run(["chmod", "-R", "u+w", str(prefix)], check=False)
    shutil.rmtree(prefix, ignore_errors=True)


def test_the_app_updates_a_system_installation_it_may_write(world, github, machine, system, qapp, wait, monkeypatch):
    """Run by root, or by whoever may write the prefix: Install now… installs for every user, under the prefix."""
    github.also = BY_HAND
    panel, window = panel_of(system / "lib/sandbox-ai/current", qapp, wait, monkeypatch)
    assert panel.data["self_updating"] is False and panel.data["install_now"] is True
    assert set(panel.data["refused"]) == {"download", "auto"}, "by itself only a user's own copy changes"
    check_install_roll_back(panel, window, wait, world, github, [system / "bin/sandbox-ai"], "system")
    assert not (machine.home / ".local/share/sandbox-ai/versions").exists(), "never as a user's installation"


def permissions_bind():
    """Whether a folder without write permission stops this user (root with its capabilities writes anyway)."""
    with tempfile.TemporaryDirectory() as folder:
        os.chmod(folder, 0o500)
        try:
            return not os.access(folder, os.W_OK)
        finally:
            os.chmod(folder, 0o700)


@pytest.mark.skipif(not permissions_bind(), reason="this user (root) may write every installation")
def test_a_system_installation_the_user_cannot_write_says_what_to_run(world, github, machine, system, qapp, wait,
                                                                      monkeypatch):
    subprocess.run(["chmod", "-R", "a-w", str(system)], check=True)
    route = backend.update_route(system / "lib/sandbox-ai/current/sandbox", euid=1000)
    assert route == ("root", str(system / "lib/sandbox-ai"))
    offer = (OLD, NEW, "root", str(system / "lib/sandbox-ai"))
    panel, window = panel_of(system / "lib/sandbox-ai/current", qapp, wait, monkeypatch, Window(offer))
    assert panel.data["install_now"] is False
    assert "sudo sandbox-ai update" in panel.state.text()
    github.publish(NEW, world.dists[NEW])
    panel.check_button.click()
    assert wait(lambda: f"{NEW} is available" in panel.state.text(), 240), panel.state.text()
    panel.install_button.click()
    assert window.terminal == ["update now"], "the window's Update now…: sudo sandbox-ai update in a terminal"
    assert version_of([system / "bin/sandbox-ai"]) == f"sandbox-ai {OLD}"
    done = run([system / "bin/sandbox-ai", "update", "--install"])
    assert done.returncode == 1 and "only root changes: sudo sandbox-ai update" in done.stderr


# -- the AppImage, the macOS app, pip ---------------------------------------------------------------------------------

needs_package = pytest.mark.skipif(not HAVE_KIT or kit.assets.arch_of("appimage", MACHINE) is None,
                                   reason=f"no package is built for {MACHINE}")


def appimage(github, machine):
    name = kit.assets.appimage(kit.assets.arch_of("appimage", MACHINE))
    image = machine.home / "Applications" / name
    image.parent.mkdir(parents=True)
    done = run(["curl", "-fsSL", "-o", image, f"{github.source}/releases/latest/download/{name}"])
    assert done.returncode == 0, done.stderr
    image.chmod(0o755)
    return image


@needs_package
def test_the_app_updates_the_appimage_it_runs_from(world, github, machine, qapp, wait, monkeypatch, request):
    image = appimage(github, machine)
    said = run([image, "cli", "version", "--verbose"])
    assert said.returncode == 0, said.stderr
    copy = next(line[len("installed in: "):] for line in said.stdout.splitlines() if line.startswith("installed in: "))
    assert "/.cache/sandbox-ai/appimage/" in copy and "self-update: yes" in said.stdout
    # The app as the AppImage starts it: sandbox-ai is the copy it unpacked, the file's name in its environment.
    monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(image))
    monkeypatch.setenv("SANDBOX_AI_PACKAGE_COPY", copy)
    # The app's own process runs from that copy (which keeps it while a newer package starts: the start of a package
    # removes the copies of other versions that nothing runs from); here a process that waits in it stands for it.
    running = subprocess.Popen(["sleep", "3600"], cwd=copy)
    request.addfinalizer(lambda: (running.kill(), running.wait()))
    assert backend.update_route(Path(copy) / "sandbox") == ("self", str(image))
    panel, window = panel_of(copy, qapp, wait, monkeypatch)
    assert panel.data["self_updating"] is True and panel.data["refused"] == {}
    before = kit.sha256(image)
    check_install_roll_back(panel, window, wait, world, github, [image, "cli"], "package")
    assert image.name in github.downloads
    assert kit.sha256(image) == before == kit.sha256(world.dists[OLD] / image.name), "back: the file kept before"


@needs_package
def test_the_app_updates_the_macos_app_it_runs_from(world, github, machine, qapp, wait, monkeypatch):
    """sandbox-ai.app in ~/Applications; hdiutil and ditto are stand-ins here (the disk image is the tests' own)."""
    app = kit.macos_app(world.dists[OLD] / kit.assets.bundle(OLD), machine.home / "Applications/sandbox-ai.app")
    tree = app / "Contents/Resources/sandbox-ai"
    assert backend.update_route(tree / "sandbox") == ("self", str(app))
    panel, window = panel_of(tree, qapp, wait, monkeypatch)
    check_install_roll_back(panel, window, wait, world, github, [app / "Contents/MacOS/sandbox-ai"], "package")
    assert kit.assets.dmg(kit.assets.arch_of("macos-app", MACHINE)) in github.downloads
    assert "hdiutil attach" in (machine.stubs / "calls").read_text()
    assert sorted(p.name for p in app.parent.iterdir()) == ["sandbox-ai.app"]


def pip_installation(world, machine):
    """A virtual environment with the wheel of OLD in it (pip install FILE)."""
    venv = machine.home / "venv"
    made = run([sys.executable, "-m", "venv", venv])
    assert made.returncode == 0, made.stderr
    done = run([venv / "bin/python", "-m", "pip", "install", "--no-index", "--no-deps", "-q",
                "--disable-pip-version-check", world.dists[OLD] / kit.assets.wheel(OLD)])
    assert done.returncode == 0, done.stderr
    return venv, next((venv / "lib").glob("python*/site-packages"))


def test_the_app_updates_the_pip_installation_it_runs_from(world, github, machine, qapp, wait, monkeypatch):
    venv, site = pip_installation(world, machine)
    # The app of a pip installation runs with the environment's Python: its commands do too.
    monkeypatch.setattr(sys, "executable", str(venv / "bin/python"))
    # (this process is the tests' Python, which may have no pip; the environment's own has one)
    monkeypatch.setattr(backend.package_updater(site / "sandbox_ai").importlib.util, "find_spec", lambda name: object())
    assert backend.update_route(site / "sandbox_ai")[0] == "self"
    panel, window = panel_of(site / "sandbox_ai", qapp, wait, monkeypatch)
    check_install_roll_back(panel, window, wait, world, github, [venv / "bin/sandbox-ai"], "pip")
    assert kit.assets.wheel(NEW) in github.downloads and kit.assets.wheel(OLD) in github.downloads, \
        "the rollback of a pip installation installs the release before again, signed"
    assert "pypi.org:443" not in github.connects, "the wheels come from the release, not from an index"


def test_a_pip_installation_the_user_cannot_write_says_what_to_run(world, github, machine, monkeypatch):
    venv, site = pip_installation(world, machine)
    monkeypatch.setattr(sys, "executable", str(venv / "bin/python"))
    monkeypatch.setattr(backend.package_updater(site / "sandbox_ai").importlib.util, "find_spec", lambda name: object())
    no = lambda path, mode: False  # noqa: E731 - (root may write everything: the access check is told so)
    assert backend.update_route(site / "sandbox_ai", access=no) == ("other", str(site))
    why = backend.update_blocker(site / "sandbox_ai", access=no)
    assert f"{site} is not writable for you" in why and why.endswith(" update") and "sudo " in why


# -- the restart: the new app takes over from the one that still runs --------------------------------------------------

def app_environment(machine, **extra):
    env = {key: value for key, value in os.environ.items()
           if key not in ("SANDBOX_AI_APP_ANOTHER", "SANDBOX_AI_HOME", "SANDBOX_AI_PACKAGE", "SANDBOX_AI_PACKAGE_COPY")}
    env.update(QT_QPA_PLATFORM="offscreen", SANDBOX_AI_APP_PYTHON=sys.executable, SANDBOX_AI_APP_FOREGROUND="1",
               SANDBOX_AI_APP_NO_INTRO="1", SANDBOX_AI_APP_NO_TOUR="1", SANDBOX_AI_APP_NO_WEBENGINE="1", **extra)
    return env


def start_app(argv, machine, **extra):
    """The app, in a process group of its own (the AppImage's start is a shell around it: stop() ends both)."""
    return subprocess.Popen([str(word) for word in argv], env=app_environment(machine, **extra),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)


def stop(*processes):
    """End the apps this test started (their own process groups; nothing else)."""
    for process in processes:
        if process is None:
            continue
        for sign in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sign)
            except (ProcessLookupError, PermissionError):
                break
            try:
                process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                continue
            if not group_alive(process.pid):
                break


def group_alive(pgid):
    try:
        os.killpg(pgid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def takes_over(machine, launcher, update, old_folder=None):
    """The app of OLD runs (LAUNCHER started it); UPDATE() installs NEW the way the app's Install now does; the app
    started again - what "Restart the app" does, and what a human does - is NEW, and the older one has quit."""
    socket = machine.runtime / instance_probe.NAME
    older = start_app(launcher, machine)
    newer = None
    try:
        found = answers(socket, OLD, 240)
        assert found is not None, ended(older, 5)
        assert found.sandbox_ai == OLD
        if old_folder is not None:
            assert found.path.startswith(str(old_folder)), found.path
        update()
        still = instance_probe.ask(socket)
        assert still is not None and still.version == OLD, "the app that runs goes on with its version"
        newer = start_app(launcher, machine)
        code, _, err = ended(older, 120)
        assert code == 0, err[-3000:]
        now = answers(socket, NEW, 240)
        assert now is not None, "the new start never became the app"
        assert now.sandbox_ai == NEW and now.pid != older.pid
        return now
    finally:
        stop(older, newer)


def test_after_the_update_the_new_app_takes_over_a_users_installation(world, github, machine):
    first = run(["bash", world.dists[OLD] / "install.sh", "--user", *APP_OFF])
    assert first.returncode == 0, first.stderr
    data = machine.home / ".local/share/sandbox-ai"
    launcher = machine.home / ".local/bin/sandbox-ai-app"
    assert launcher.is_symlink() and os.readlink(launcher) == str(data / "current/sandbox-ai-app")
    github.publish(NEW, world.dists[NEW])
    github.also = BY_HAND

    def update():
        done = run([machine.home / ".local/bin/sandbox-ai", "update", "--install"])
        assert done.returncode == 0 and f"installed {NEW} (was {OLD})" in done.stdout, done.stderr
    now = takes_over(machine, [launcher], update, data / "versions" / OLD)
    assert now.path.startswith(str(data / "versions" / NEW))
    # "Restart the app" starts the installation's stable launcher, which is the new version's from now on.
    assert backend.restart_command(4321, data / "versions" / OLD / "sandbox") == [str(data / "current/sandbox-ai-app"),
                                                                                    "--wait-for-pid=4321"]


@needs_package
def test_after_the_update_the_new_app_takes_over_from_the_appimage(world, github, machine):
    image = appimage(github, machine)
    github.publish(NEW, world.dists[NEW])

    def update():
        done = run([image, "cli", "update", "--install"])
        assert done.returncode == 0 and f"installed {NEW} (was {OLD})" in done.stdout, done.stdout + done.stderr
    now = takes_over(machine, [image], update, machine.home / ".cache/sandbox-ai/appimage")
    assert f"/appimage/{NEW}-" in now.path, now.path
    assert kit.sha256(image) == kit.sha256(world.dists[NEW] / image.name)
