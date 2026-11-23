"""Updates over GitHub, end to end, for every kind of installation: install.sh's (a user's own, a system one, one
made from a checkout, the last public release 0.1.94), the .pyz, the AppImage, the macOS app and a pip installation.

The release is a real one (tests/release_kit.py: scripts/make_dist.py over a copy of this checkout, signed with a test
key; stand-ins only for what CI alone can build), served by tests/fake_github.py as https://github.com/c0decave/
sandbox-ai - the default source - to the real, unchanged code: every test starts the commands a user types
(install.sh, sandbox-ai update, ./sandbox-ai.pyz update, ...).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import fake_github  # noqa: E402
import release_kit as kit  # noqa: E402

pytestmark = pytest.mark.skipif(not (fake_github.available() and shutil.which("ssh-keygen")),
                                reason="needs the openssl and ssh-keygen commands")

OLD, NEW = kit.VERSION, kit.later(kit.VERSION)
GOLDEN = Path(__file__).resolve().parent / "golden" / "release-0.1.94"
LAST_PUBLIC = "0.1.94"
NEWEST_PUBLIC = "0.1.188"  # the newest release on GitHub (2026-11-21): what releases/latest serves until the next one


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """Two signed releases of this checkout, OLD (its version) and NEW (the next one), every file of each."""
    base = tmp_path_factory.mktemp("releases")
    key, evil = kit.new_key(base / "keys"), kit.new_key(base / "keys", "evil")
    trees, dists = {}, {}
    for version in (OLD, NEW):
        trees[version] = kit.tree_copy(base / f"tree-{version}", version, [key])
        dists[version] = kit.full_release(trees[version], base / f"dist-{version}", key, base / "work")
    yield SimpleNamespace(base=base, key=key, evil=evil, trees=trees, dists=dists)
    kit.remove(base)  # (some 300 MB)


@pytest.fixture(autouse=True)
def nothing_left(tmp_path):
    """Each test's folder goes when it ends: the installations, the packages and the read-only copies they unpacked
    (some 100 MB a test; pytest keeps the folders of three runs otherwise)."""
    yield
    kit.remove(tmp_path)


@pytest.fixture
def github(world):
    fake = fake_github.FakeGitHub(world.base / "tls")
    fake.publish(OLD, world.dists[OLD])
    fake.also = set()  # (hosts a test expects to be asked for, and refused, beside GitHub)
    yield fake
    fake.close()
    assert fake.leaked == [], "a token went to the files' host"
    assert set(fake.refused) <= fake.also, f"something other than GitHub was asked: {fake.refused}"


@pytest.fixture
def stubs(tmp_path):
    """systemctl and launchctl that say yes and change nothing (update mode's timer), first on PATH."""
    folder = tmp_path / "stubs"
    folder.mkdir()
    for name in ("systemctl", "launchctl", "notify-send", "osascript"):
        (folder / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{folder}/calls"\nexit 0\n')
        (folder / name).chmod(0o755)
    return folder


def environment(tmp_path, github, stubs=None, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_", "PYTHON"))}
    env = github.env(env)
    env.update(HOME=str(home), PATH=(f"{stubs}:" if stubs else "") + "/usr/bin:/bin", TMPDIR=str(tmp_path),
               NO_COLOR="1", **extra)
    return env


def run(argv, env, seconds=300, **kwargs):
    return subprocess.run([str(word) for word in argv], capture_output=True, text=True, env=env, timeout=seconds,
                          stdin=subprocess.DEVNULL, **kwargs)


def version_of(command, env):
    done = run([*command, "--version"], env)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


APP_OFF = ["--no-app-deps", "--no-app-terminal"]  # (PySide6 is never fetched here: no PyPI in these tests)


# -- install.sh's installations ---------------------------------------------------------------------------------------

def test_a_users_installation_installs_and_updates_from_github(tmp_path, world, github, stubs):
    """curl .../install.sh | bash, as the README says it - then sandbox-ai update --check, update, --rollback."""
    env = environment(tmp_path, github, stubs)
    # The published install.sh (it carries the release keys), through curl and the redirect of releases/latest.
    script = tmp_path / "install.sh"
    fetched = run(["curl", "-fsSL", "-o", script, f"{github.source}/releases/latest/download/install.sh"], env)
    assert fetched.returncode == 0, fetched.stderr
    first = run(["bash", "-c", f'cat "{script}" | bash -s -- --user {" ".join(APP_OFF)}'], env)
    assert first.returncode == 0, first.stderr
    assert f"downloaded sandbox-ai {OLD} from https://github.com/c0decave/sandbox-ai (SHA-256 verified, signed by " \
           f"{world.key.fingerprint})" in first.stderr
    command = [tmp_path / "home/.local/bin/sandbox-ai"]
    assert version_of(command, env) == f"sandbox-ai {OLD}"

    check = run([*command, "update", "--check"], env)
    assert check.returncode == 0 and f"available: {OLD} " in check.stdout, check.stderr
    github.publish(NEW, world.dists[NEW])
    check = run([*command, "update", "--check"], env)
    assert check.returncode == 0, check.stderr
    assert f"installed: {OLD}" in check.stdout and f"available: {NEW} (https://github.com/c0decave/sandbox-ai)" in \
        check.stdout and f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout

    github.downloads.clear()
    update = run([*command, "update", *APP_OFF], env)
    assert update.returncode == 0, update.stderr
    assert f"installed sandbox-ai {NEW} (was {OLD})" in update.stderr
    assert version_of(command, env) == f"sandbox-ai {NEW}"
    assert sorted(github.downloads) == sorted([kit.assets.bundle(NEW), "SHA256SUMS", "SHA256SUMS.sig"]), \
        "an install.sh installation takes the bundle, its checksums and their signature - nothing else"
    assert {host for _, host, _, _ in github.requests} <= set(fake_github.HOSTS)

    back = run([*command, "update", "--rollback"], env)
    assert back.returncode == 0 and f"back to {OLD} (was {NEW})" in back.stdout, back.stderr
    assert version_of(command, env) == f"sandbox-ai {OLD}"


def test_update_mode_download_then_apply_from_github(tmp_path, world, github, stubs):
    """The update mode's own path: the timer's run fetches and checks the release, --apply installs it."""
    env = environment(tmp_path, github, stubs)
    assert run(["bash", world.dists[OLD] / "install.sh", "--user", *APP_OFF], env).returncode == 0
    command = [tmp_path / "home/.local/bin/sandbox-ai"]
    mode = run([*command, "update", "mode", "download", "--json"], env)
    assert mode.returncode == 0 and json.loads(mode.stdout)["mode"] == "download", mode.stderr
    github.publish(NEW, world.dists[NEW])
    tick = run([*command, "update", "--scheduled"], env)
    assert tick.returncode == 0, tick.stdout + tick.stderr
    assert f"downloaded and checked: {NEW}" in tick.stdout
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["downloaded"] == NEW and status["installed"] == OLD and status["result"]["newer"] is True
    assert status["source"] == "https://github.com/c0decave/sandbox-ai"
    # By hand, install.sh also looks after the app's PySide6 (python3 has none here): PyPI is asked, the fake refuses
    # it, and the installation is complete without (the timer's own install never asks PyPI: --no-app-deps).
    github.also = {"pypi.org:443"}
    applied = run([*command, "update", "--apply"], env)
    assert applied.returncode == 0 and f"installed {NEW} (was {OLD})" in applied.stdout, applied.stderr
    assert version_of(command, env) == f"sandbox-ai {NEW}"
    assert json.loads(run([*command, "update", "mode", "--json"], env).stdout)["rollback"] == OLD


def system_prefix():
    """A folder for a system installation that is in nobody's home (install.sh's and the launcher's HOMES) and where
    programs may run (no noexec mount), or None."""
    for base in ("/var/tmp", "/tmp", tempfile.gettempdir(), "/dev/shm"):
        if not os.access(base, os.W_OK) or base.startswith(("/home/", "/root/", "/Users/", "/var/home/")):
            continue
        folder = Path(tempfile.mkdtemp(prefix="sandbox-ai-system-", dir=base))
        probe = folder / "probe"
        probe.write_text("#!/bin/sh\nexit 0\n")
        probe.chmod(0o755)
        try:
            runs = subprocess.run([str(probe)], capture_output=True).returncode == 0
        except OSError:
            runs = False
        probe.unlink()
        if runs:
            return folder
        shutil.rmtree(folder, ignore_errors=True)
    return None


def test_a_system_installation_updates_from_github(tmp_path, world, github, stubs):
    prefix = system_prefix()
    if prefix is None:
        pytest.skip("no folder outside the homes to install into")
    try:
        env = environment(tmp_path, github, stubs)
        first = run(["bash", world.dists[OLD] / "install.sh", "--system", "--prefix", prefix, *APP_OFF], env)
        assert first.returncode == 0, first.stderr
        command = [prefix / "bin/sandbox-ai"]
        verbose = run([*command, "version", "--verbose"], env).stdout
        assert f"sandbox-ai {OLD}" in verbose and str(prefix / "lib/sandbox-ai") in verbose
        github.publish(NEW, world.dists[NEW])
        check = run([*command, "update", "--check"], env)
        assert check.returncode == 0 and f"available: {NEW} " in check.stdout, check.stderr
        update = run([*command, "update", *APP_OFF], env)
        assert update.returncode == 0, update.stderr
        assert f"installed sandbox-ai {NEW} (was {OLD}) in {prefix}/lib/sandbox-ai" in update.stderr
        assert version_of(command, env) == f"sandbox-ai {NEW}"
        assert not (tmp_path / "home/.local/share/sandbox-ai/versions").exists(), "updated as a user installation"
        back = run([*command, "update", "--rollback"], env)
        assert back.returncode == 0, back.stderr
        assert version_of(command, env) == f"sandbox-ai {OLD}"
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(prefix)], check=False)
        shutil.rmtree(prefix, ignore_errors=True)


def permissions_bind():
    """Whether a folder without write permission stops this user (root with its capabilities writes anyway)."""
    with tempfile.TemporaryDirectory() as folder:
        os.chmod(folder, 0o500)
        try:
            return not os.access(folder, os.W_OK)
        finally:
            os.chmod(folder, 0o700)


@pytest.mark.skipif(not permissions_bind(), reason="this user (root) may write every installation")
def test_a_system_installation_its_user_cannot_write_is_checked_and_says_who_updates_it(tmp_path, world, github, stubs):
    """Installed by root for everybody: each user's check (the app's, the daily one of notify) works - it found a
    signed release and failed on remembering it in the installation (release-state.json) -, and an update by them
    says the command."""
    prefix = system_prefix()
    if prefix is None:
        pytest.skip("no folder outside the homes to install into")
    try:
        env = environment(tmp_path, github, stubs)
        first = run(["bash", world.dists[OLD] / "install.sh", "--system", "--prefix", prefix, *APP_OFF], env)
        assert first.returncode == 0, first.stderr
        command = [prefix / "bin/sandbox-ai"]
        github.publish(NEW, world.dists[NEW])
        subprocess.run(["chmod", "-R", "a-w", str(prefix)], check=True)
        check = run([*command, "update", "--check"], env)
        assert check.returncode == 0, check.stderr
        assert "Traceback" not in check.stderr and "PermissionError" not in check.stderr
        assert f"installed: {OLD}" in check.stdout and f"available: {NEW} " in check.stdout
        assert f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout
        status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
        assert status["kind"] == "system" and status["result"]["newer"] is True and status["result"]["error"] is None
        assert status["self_updating"] is False and status["install_now"] is False
        assert status["manual"].startswith("sudo sandbox-ai update")
        install = run([*command, "update", "--install"], env)
        assert install.returncode == 1 and "which only root changes: sudo sandbox-ai update" in install.stderr
        back = run([*command, "update", "--rollback"], env)
        assert back.returncode == 1 and "Traceback" not in back.stderr
        assert version_of(command, env) == f"sandbox-ai {OLD}"
    finally:
        subprocess.run(["chmod", "-R", "u+w", str(prefix)], check=False)
        shutil.rmtree(prefix, ignore_errors=True)


def test_an_installation_from_a_checkout_updates_from_github(tmp_path, world, github, stubs):
    """./install.sh --from . (no release involved), then the installed copy's own update over GitHub."""
    env = environment(tmp_path, github, stubs)
    first = run(["bash", world.trees[OLD] / "install.sh", "--user", "--from", world.trees[OLD], *APP_OFF], env,
                cwd=world.trees[OLD])
    assert first.returncode == 0, first.stderr
    command = [tmp_path / "home/.local/bin/sandbox-ai"]
    github.publish(NEW, world.dists[NEW])
    update = run([*command, "update", *APP_OFF], env)
    assert update.returncode == 0, update.stderr
    assert f"signed by {world.key.fingerprint}" in update.stderr
    assert version_of(command, env) == f"sandbox-ai {NEW}"


def last_public_installation(tmp_path, world, full, release=LAST_PUBLIC):
    """~/.local/share/sandbox-ai as install.sh of the last public release (0.1.94) left it: its own install.sh, main.py
    and launcher (tests/golden/release-0.1.94, the files of the tag v0.1.94) - FULL: the whole tree of that tag (or of
    RELEASE's), from this repository's history - trusting the tests' release key as the real one trusts the real one."""
    data = tmp_path / "home/.local/share/sandbox-ai"
    tree = data / "versions" / release
    if full:
        tree.mkdir(parents=True)
        archive = subprocess.run(["git", "-C", str(kit.REPOSITORY), "archive", f"v{release}"],
                                 capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    else:
        (tree / "sandbox").mkdir(parents=True)
        shutil.copy2(GOLDEN / "install.sh", tree / "install.sh")
        shutil.copy2(GOLDEN / "sandbox-ai", tree / "sandbox-ai")
        shutil.copy2(GOLDEN / "main.py", tree / "sandbox/main.py")
        (tree / "sandbox/agent_sandbox.py").write_text(f'"""sandbox-ai"""\n__version__ = "{LAST_PUBLIC}"\n')
    (tree / "sandbox/release-signers").write_text(f"# the tests' key\n{world.key.line} release\n")
    os.symlink(f"versions/{release}", data / "current")
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    os.symlink(data / "current/sandbox-ai", bin_dir / "sandbox-ai")
    os.symlink(data / "current/install.sh", bin_dir / "sandbox-ai-update")
    return bin_dir / "sandbox-ai"


def has_tag(tag):
    return subprocess.run(["git", "-C", str(kit.REPOSITORY), "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
                          capture_output=True).returncode == 0


@pytest.mark.parametrize("full", [False, pytest.param(True, marks=pytest.mark.skipif(
    not has_tag(f"v{LAST_PUBLIC}"), reason="this checkout has no tag v0.1.94"))], ids=["its-updater", "its-whole-tree"])
def test_the_last_public_release_updates_to_the_next_one_over_github(full, tmp_path, world, github, stubs):
    """0.1.94 is what GitHub's releases/latest served last: its `sandbox-ai update` must take the next release."""
    env = environment(tmp_path, github, stubs)
    command = last_public_installation(tmp_path, world, full)
    assert version_of([command], env) == f"sandbox-ai {LAST_PUBLIC}"
    github.publish(NEW, world.dists[NEW])
    check = run([command, "update", "--check"], env)
    assert check.returncode == 0, check.stderr
    assert f"installed: {LAST_PUBLIC}" in check.stdout and f"available: {NEW} " in check.stdout
    assert f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout
    update = run([command, "update", *APP_OFF], env)
    assert update.returncode == 0, update.stderr
    assert f"installed sandbox-ai {NEW} (was {LAST_PUBLIC})" in update.stderr
    assert version_of([command], env) == f"sandbox-ai {NEW}"
    # ... and what it became updates itself from then on (the new release's own updater).
    assert run([command, "update", "--check"], env).returncode == 0
    assert (tmp_path / "home/.local/bin/sandbox-ai-update").resolve().name == "install.sh"


@pytest.mark.skipif(not has_tag(f"v{NEWEST_PUBLIC}"), reason=f"this checkout has no tag v{NEWEST_PUBLIC}")
def test_the_newest_public_release_updates_to_the_next_one_over_github(tmp_path, world, github, stubs):
    """0.1.188 is what GitHub's releases/latest serves now: the whole tree of its tag, installed as its install.sh
    leaves it, must see the next release signed and take it with its own updater (its install.sh and update code,
    not this checkout's)."""
    env = environment(tmp_path, github, stubs)
    command = last_public_installation(tmp_path, world, True, NEWEST_PUBLIC)
    assert version_of([command], env) == f"sandbox-ai {NEWEST_PUBLIC}"
    github.publish(NEW, world.dists[NEW])
    check = run([command, "update", "--check"], env)
    assert check.returncode == 0, check.stdout + check.stderr
    assert f"installed: {NEWEST_PUBLIC}" in check.stdout and f"available: {NEW} " in check.stdout
    assert f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout
    update = run([command, "update", *APP_OFF], env)
    assert update.returncode == 0, update.stdout[-3000:] + update.stderr[-3000:]
    assert f"installed sandbox-ai {NEW} (was {NEWEST_PUBLIC})" in update.stderr
    assert version_of([command], env) == f"sandbox-ai {NEW}"
    again = run([command, "update", "--check"], env)
    assert again.returncode == 0 and f"installed: {NEW}" in again.stdout, again.stdout + again.stderr


@pytest.fixture(scope="module")
def newest_public(world):
    """The release files of the tag v0.1.188 as its own make_dist.py builds them (its .pyz, wheel, AppImage layout),
    trusting the tests' key: the copies people run now, with their own update code."""
    base = world.base / f"public-{NEWEST_PUBLIC}"
    tree = base / "tree"
    tree.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(kit.REPOSITORY), "archive", f"v{NEWEST_PUBLIC}"],
                             capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    kit.set_keys(tree, [world.key])
    kit.commit(tree, f"v{NEWEST_PUBLIC}")
    dist = kit.make_dist(tree, base / "dist")
    kit.fake_appimage(tree, dist / kit.assets.bundle(NEWEST_PUBLIC), dist, kit.assets.APPIMAGE_ARCHES[0])
    yield dist
    kit.remove(base)


def newest_public_takes_the_next(command, env, github, world):
    assert version_of(command, env) == f"sandbox-ai {NEWEST_PUBLIC}"
    github.publish(NEW, world.dists[NEW])
    check = run([*command, "update", "--check"], env)
    assert check.returncode == 0, check.stdout + check.stderr
    assert f"installed: {NEWEST_PUBLIC}" in check.stdout and f"available: {NEW} " in check.stdout
    assert f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout
    update = run([*command, "update"], env)
    assert update.returncode == 0, update.stdout[-3000:] + update.stderr[-3000:]
    assert f"installed {NEW} (was {NEWEST_PUBLIC})" in update.stdout + update.stderr
    assert version_of(command, env) == f"sandbox-ai {NEW}"


@pytest.mark.skipif(not has_tag(f"v{NEWEST_PUBLIC}"), reason=f"this checkout has no tag v{NEWEST_PUBLIC}")
@pytest.mark.parametrize("kind", ["pyz", "pip", "appimage"])
def test_the_newest_public_package_replaces_itself_with_the_next_release(kind, tmp_path, world, github, stubs,
                                                                         newest_public):
    """0.1.188's .pyz, wheel and AppImage (its own dist_boot.py and package_update.py) take the next release."""
    env = environment(tmp_path, github, stubs)
    if kind == "pyz":
        pyz = tmp_path / "home/bin/sandbox-ai.pyz"
        pyz.parent.mkdir(parents=True)
        shutil.copy2(newest_public / "sandbox-ai.pyz", pyz)
        command = [pyz]
    elif kind == "pip":
        venv = tmp_path / "home/venv"
        assert run([sys.executable, "-m", "venv", venv], env).returncode == 0
        done = run([venv / "bin/python", "-m", "pip", "install", "--no-index", "--no-deps", "-q",
                    newest_public / kit.assets.wheel(NEWEST_PUBLIC)], env)
        assert done.returncode == 0, done.stderr
        command = [venv / "bin/sandbox-ai"]
    else:
        if kit.assets.arch_of("appimage", MACHINE) != kit.assets.APPIMAGE_ARCHES[0]:
            pytest.skip(f"the stand-in AppImage is built for {kit.assets.APPIMAGE_ARCHES[0]}")
        name = kit.assets.appimage(kit.assets.APPIMAGE_ARCHES[0])
        image = tmp_path / "home/Applications" / name
        image.parent.mkdir(parents=True)
        shutil.copy2(newest_public / name, image)
        command = [image, "cli"]
    newest_public_takes_the_next(command, env, github, world)


# -- the packages: the .pyz, the AppImage, the macOS app --------------------------------------------------------------

MACHINE = os.uname().machine
needs_package = pytest.mark.skipif(kit.assets.arch_of("appimage", MACHINE) is None,
                                   reason=f"no package is built for {MACHINE}")


def latest(github, env, name, target):
    """NAME of the newest release, downloaded as the release page says (curl -fLO .../releases/latest/download/NAME)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    done = run(["curl", "-fsSL", "-o", target, f"{github.source}/releases/latest/download/{name}"], env)
    assert done.returncode == 0, done.stderr
    target.chmod(0o755)
    return target


def self_update(command, env, world, github, file_name, path, again=True):
    """What every copy that replaces itself does, through its own command line COMMAND: --check, update, the new
    version, the files it asked GitHub for, --rollback, and (AGAIN) the update again."""
    assert version_of(command, env) == f"sandbox-ai {OLD}"
    quiet = run([*command, "update"], env)
    assert quiet.returncode == 0 and f"sandbox-ai {OLD} is already installed and up to date" in quiet.stdout + \
        quiet.stderr, quiet.stderr
    github.publish(NEW, world.dists[NEW])
    check = run([*command, "update", "--check"], env)
    assert check.returncode == 0, check.stderr
    assert f"installed: {OLD}" in check.stdout and f"available: {NEW} (https://github.com/c0decave/sandbox-ai)" in \
        check.stdout and f"signature: good, by the trusted key {world.key.fingerprint}" in check.stdout
    assert "update: sandbox-ai update" in check.stdout and "download the new" not in check.stdout
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["self_updating"] is True and status["manual"] is None and status["refused"] == {}
    github.downloads.clear()
    update = run([*command, "update"], env)
    assert update.returncode == 0, update.stdout + update.stderr
    assert f"downloaded {file_name} of sandbox-ai {NEW} from https://github.com/c0decave/sandbox-ai (SHA-256 " \
           f"verified, signed by {world.key.fingerprint})" in update.stdout
    assert f"installed {NEW} (was {OLD})" in update.stdout
    assert sorted(github.downloads) == sorted([file_name, "SHA256SUMS", "SHA256SUMS.sig"])
    assert version_of(command, env) == f"sandbox-ai {NEW}"
    if Path(path).is_file():
        assert kit.sha256(path) == kit.sha256(world.dists[NEW] / file_name), "the release's file, byte for byte"
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["installed"] == NEW and status["rollback"] == OLD and status["last_install"]["to"] == NEW
    back = run([*command, "update", "--rollback"], env)
    assert back.returncode == 0 and f"back to {OLD} (was {NEW})" in back.stdout, back.stderr
    assert version_of(command, env) == f"sandbox-ai {OLD}"
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["rollback"] == NEW and status["skipped"] == NEW, "forward again is possible; auto leaves NEW out"
    if again:  # (by hand the version left is taken again)
        forward = run([*command, "update"], env)
        assert forward.returncode == 0, forward.stderr
        assert version_of(command, env) == f"sandbox-ai {NEW}"
    return path


def test_the_pyz_replaces_itself_from_github(tmp_path, world, github, stubs):
    env = environment(tmp_path, github, stubs)
    pyz = latest(github, env, "sandbox-ai.pyz", tmp_path / "home/bin/sandbox-ai.pyz")
    self_update([pyz], env, world, github, "sandbox-ai.pyz", pyz, again=False)
    # (each start of another version unpacks it once, some 20 s: the update again is the AppImage's and the app's)
    assert kit.sha256(pyz) == kit.sha256(world.dists[OLD] / "sandbox-ai.pyz"), "back: the file kept before"
    assert not [p.name for p in pyz.parent.iterdir() if p.name != "sandbox-ai.pyz"], "nothing is left beside it"


@needs_package
def test_the_appimage_replaces_itself_from_github(tmp_path, world, github, stubs):
    env = environment(tmp_path, github, stubs)
    name = kit.assets.appimage(kit.assets.arch_of("appimage", MACHINE))
    image = latest(github, env, name, tmp_path / "home/Applications" / name)
    self_update([image, "cli"], env, world, github, name, image)
    assert kit.sha256(image) == kit.sha256(world.dists[NEW] / name)
    assert os.access(image, os.X_OK)
    # Called by the name sandbox-ai (a link, as the README says) it is the command line, and updates the file.
    link = image.parent / "sandbox-ai"
    os.symlink(name, link)
    assert version_of([link], env) == f"sandbox-ai {NEW}"


@needs_package
def test_an_unpacked_appimage_says_what_to_download(tmp_path, world, github, stubs):
    """--appimage-extract, then AppRun's part by hand: no file to replace."""
    env = environment(tmp_path, github, stubs)
    name = kit.assets.appimage(kit.assets.arch_of("appimage", MACHINE))
    image = latest(github, env, name, tmp_path / "home/Applications" / name)
    assert run([image, "--appimage-extract"], env, cwd=tmp_path).returncode == 0
    command = ["python3", "-I", tmp_path / "squashfs-root/usr/lib/sandbox-ai/boot.py", "cli"]
    github.publish(NEW, world.dists[NEW])
    update = run([*command, "update"], env)
    assert update.returncode == 1
    assert "runs from an unpacked AppImage" in update.stderr and f"download {name} from the release page" in \
        update.stderr
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["self_updating"] is False and "unpacked AppImage" in status["manual"]
    assert set(status["refused"]) == {"download", "auto"}


@needs_package
def test_the_macos_app_replaces_itself_from_github(tmp_path, world, github, stubs):
    """sandbox-ai.app in ~/Applications, replaced with the one in the release's disk image (hdiutil and ditto are
    stand-ins here; release.yml's macOS jobs run the real ones)."""
    kit.hdiutil_stub(stubs)
    env = environment(tmp_path, github, stubs)
    name = kit.assets.dmg(kit.assets.arch_of("macos-app", MACHINE))
    app = kit.macos_app(world.dists[OLD] / kit.assets.bundle(OLD), tmp_path / "home/Applications/sandbox-ai.app")
    self_update([app / "Contents/MacOS/sandbox-ai"], env, world, github, name, app)
    calls = (stubs / "calls").read_text()
    assert "hdiutil attach -nobrowse -readonly -noautoopen -mountpoint" in calls and "hdiutil detach" in calls
    assert "ditto " in calls
    assert sorted(p.name for p in app.parent.iterdir()) == ["sandbox-ai.app"], "nothing is left beside the app"
    assert (app / "Contents/Resources/sandbox-ai/PACKAGING").read_text() == "macos-app\n"


@needs_package
@pytest.mark.skipif(not permissions_bind(), reason="this user (root) may write every folder")
def test_a_package_in_a_folder_its_user_cannot_write_says_the_command_that_can(tmp_path, world, github, stubs):
    """The AppImage in /opt, say: the update is whoever's who may write there, and the exact command is said."""
    env = environment(tmp_path, github, stubs)
    name = kit.assets.appimage(kit.assets.arch_of("appimage", MACHINE))
    image = latest(github, env, name, tmp_path / "opt" / name)
    assert version_of([image, "cli"], env) == f"sandbox-ai {OLD}"
    github.publish(NEW, world.dists[NEW])
    image.parent.chmod(0o555)
    try:
        before = kit.sha256(image)
        github.downloads.clear()
        update = run([image, "cli", "update"], env)
        assert update.returncode == 1, update.stdout
        assert f"{image.parent} is not writable for you; run the update as someone who may write it: sudo -H " \
               f"{image} cli update" in update.stderr
        assert github.downloads == [] and kit.sha256(image) == before, "nothing is downloaded, nothing changes"
        verbose = run([image, "cli", "version", "--verbose"], env).stdout
        assert "self-update: no" in verbose and f"sudo -H {image} cli update" in verbose
        status = json.loads(run([image, "cli", "update", "mode", "--json"], env).stdout)
        assert status["self_updating"] is False and status["install_now"] is False
        assert f"sudo -H {image} cli update" in status["manual"] and set(status["refused"]) == {"download", "auto"}
        check = run([image, "cli", "update", "--check"], env)  # (looking is everybody's)
        assert check.returncode == 0 and f"available: {NEW} " in check.stdout and "update: " in check.stdout
        assert f"sudo -H {image} cli update" in check.stdout
    finally:
        image.parent.chmod(0o755)


# -- pip ---------------------------------------------------------------------------------------------------------------

def pip_environment(tmp_path, world, env):
    """A virtual environment with the wheel of OLD, installed as the README says (pip install FILE; no index)."""
    venv = tmp_path / "home/venv"
    made = run([sys.executable, "-m", "venv", venv], env)
    assert made.returncode == 0, made.stderr
    wheel = world.dists[OLD] / kit.assets.wheel(OLD)
    done = run([venv / "bin/python", "-m", "pip", "install", "--no-index", "--no-deps", "-q", wheel], env)
    assert done.returncode == 0, done.stderr
    return venv


def test_a_pip_installation_updates_from_github(tmp_path, world, github, stubs):
    env = environment(tmp_path, github, stubs)
    venv = pip_environment(tmp_path, world, env)
    command = [venv / "bin/sandbox-ai"]
    status = json.loads(run([*command, "update", "mode", "--json"], env).stdout)
    assert status["kind"] == "pip" and status["installed"] == OLD, "a pip installation knows its version"
    self_update(command, env, world, github, kit.assets.wheel(NEW), venv)
    listed = run([venv / "bin/python", "-m", "pip", "list", "--format=json", "--disable-pip-version-check"], env)
    assert {"name": "sandbox-ai-app", "version": NEW} in json.loads(listed.stdout)
    assert "pypi.org:443" not in github.connects, "the wheel comes from the release, not from an index"


def test_a_pip_installation_of_the_old_package_name_says_what_to_run(tmp_path, world, github, stubs):
    """0.1.94 and before were the package sandbox-ai (sandbox_ai-0.1.94-py3-none-any.whl): the new wheel is another
    package, so it is never installed over it."""
    env = environment(tmp_path, github, stubs)
    venv = pip_environment(tmp_path, world, env)
    site = next((venv / "lib").glob("python*/site-packages"))
    record = next(site.glob("sandbox_ai_app-*.dist-info"))
    record.rename(site / "sandbox_ai-0.1.94.dist-info")
    github.publish(NEW, world.dists[NEW])
    update = run([venv / "bin/sandbox-ai", "update"], env)
    assert update.returncode == 1
    assert "-m pip uninstall sandbox-ai, then" in update.stderr and "-m pip install sandbox-ai-app" in update.stderr


# -- what is refused, for every copy that replaces itself -------------------------------------------------------------

def resigned(world, tmp_path, signer=None, change=None, drop=()):
    """The files of NEW with one thing wrong: CHANGE (a name: that file's bytes), SHA256SUMS signed by SIGNER (None:
    not at all), DROP left out."""
    files = {path.name: path for path in world.dists[NEW].iterdir() if path.name not in drop}
    if change:
        bad = tmp_path / f"changed-{change}"
        bad.write_bytes(files[change].read_bytes() + b"\n# changed\n")
        files[change] = bad
    files.pop("SHA256SUMS.sig", None)
    if signer is not None:
        signature = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(signer.path), "-n", "sandbox-ai-release"],
                                   input=files["SHA256SUMS"].read_bytes(), capture_output=True, check=True).stdout
        files["SHA256SUMS.sig"] = signature
    return files


@pytest.mark.parametrize("case, words", [
    ("unsigned", "is not signed (no SHA256SUMS.sig); nothing updated"),
    ("another-key", "which is not a trusted release key; nothing updated"),
    ("changed-file", "checksum mismatch for sandbox-ai.pyz; nothing updated"),
    ("no-file", "has no sandbox-ai.pyz"),
    ("older", "is older than the installed"),
])
def test_the_pyz_refuses_what_is_not_the_signed_release(case, words, tmp_path, world, github, stubs):
    # (one cache for the cases: nothing is replaced in any of them, and a first start unpacks for some 20 s)
    env = environment(tmp_path, github, stubs, XDG_CACHE_HOME=str(world.base / "cache-refusals"))
    if case == "older":
        github.releases.clear()
        github.publish(NEW, world.dists[NEW])
        pyz = latest(github, env, "sandbox-ai.pyz", tmp_path / "home/bin/sandbox-ai.pyz")
        # Never seen a release there: a copy newer than the newest release has nothing to update (said, status 0).
        # (the cases share one cache: what the others saw is forgotten here)
        shutil.rmtree(world.base / "cache-refusals" / "sandbox-ai" / "update-seen", ignore_errors=True)
        github.publish(OLD, world.dists[OLD])
        plain = run([pyz, "update"], env)
        assert plain.returncode == 0 and f"sandbox-ai {NEW} is newer than the newest release {OLD}: nothing to " \
            "update" in plain.stdout, plain.stdout + plain.stderr
        github.releases.clear()
        github.publish(NEW, world.dists[NEW])
        assert run([pyz, "update", "--check"], env).returncode == 0  # (NEW seen there, signed)
        github.publish(OLD, world.dists[OLD])  # (then the source serves the older release as its newest: a replay)
    else:
        pyz = latest(github, env, "sandbox-ai.pyz", tmp_path / "home/bin/sandbox-ai.pyz")
        github.publish(NEW, {"unsigned": lambda: resigned(world, tmp_path),
                             "another-key": lambda: resigned(world, tmp_path, world.evil),
                             "changed-file": lambda: resigned(world, tmp_path, world.key, change="sandbox-ai.pyz"),
                             "no-file": lambda: resigned(world, tmp_path, world.key, drop=("sandbox-ai.pyz",)),
                             }[case]())
    before = kit.sha256(pyz)
    update = run([pyz, "update"], env)
    assert update.returncode == 1, update.stdout
    assert words in update.stderr, update.stderr
    assert kit.sha256(pyz) == before, "the file stays as it was"
    assert "Traceback" not in update.stderr


def test_update_mode_download_for_a_package_then_apply(tmp_path, world, github, stubs):
    """The .pyz with update mode download: the timer runs the file itself, its run fetches and checks the release,
    --apply installs it; a file changed after the download is refused."""
    env = environment(tmp_path, github, stubs)
    pyz = latest(github, env, "sandbox-ai.pyz", tmp_path / "home/bin/sandbox-ai.pyz")
    mode = run([pyz, "update", "mode", "download", "--json"], env)
    assert mode.returncode == 0, mode.stderr
    unit = (tmp_path / "home/.config/systemd/user/sandbox-ai-update.service").read_text()
    assert f'"{pyz}" "update" "--scheduled"' in unit, "the timer runs the file, not the copy of one version"
    github.publish(NEW, world.dists[NEW])
    tick = run([pyz, "update", "--scheduled"], env)
    assert tick.returncode == 0 and f"downloaded and checked: {NEW}" in tick.stdout, tick.stdout + tick.stderr
    assert version_of([pyz], env) == f"sandbox-ai {OLD}", "download installs nothing"
    status = json.loads(run([pyz, "update", "mode", "--json"], env).stdout)
    assert status["downloaded"] == NEW
    pending = tmp_path / "home/.cache/sandbox-ai/update-pending/pyz/sandbox-ai.pyz"
    kept = pending.read_bytes()
    pending.write_bytes(kept + b"x")
    refused = run([pyz, "update", "--apply"], env)
    assert refused.returncode == 1 and "checksum mismatch for the downloaded sandbox-ai.pyz" in refused.stderr
    assert version_of([pyz], env) == f"sandbox-ai {OLD}"
    assert not pending.exists(), "what failed the check is removed"
    again = run([pyz, "update", "--scheduled"], env)  # (the timer's next run fetches it anew)
    assert again.returncode == 0 and f"downloaded and checked: {NEW}" in again.stdout, again.stdout + again.stderr
    applied = run([pyz, "update", "--apply"], env)
    assert applied.returncode == 0 and f"installed {NEW} (was {OLD})" in applied.stdout, applied.stderr
    assert version_of([pyz], env) == f"sandbox-ai {NEW}"
