"""Every update path where it went wrong or said nothing (the user 2026-11-22: "update pfade genau untersuchen und
fixen wo notwendig"), against the recorded real GitHub release 0.1.188 (tests/golden/github-release-0.1.188, served by
tests/fake_github.py as https://github.com/c0decave/sandbox-ai; no network).

The user's own case: a system installation (install.sh --system) newer than the newest release - 0.1.193 against
0.1.188 then. Every entry point must say so plainly ("newer than the newest release: nothing to update"), never stay
silent, never call it an attack, never fail.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import fake_github  # noqa: E402
import release_kit as kit  # noqa: E402
from test_update_recorded_release import RECORDED, RELEASE, environment, files, recorded, run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
INSTALLED = kit.VERSION  # this checkout: newer than the recorded release
APP_OFF = ["--no-app-deps", "--no-app-terminal", "--no-modify-rc"]
NOTHING = f"sandbox-ai {INSTALLED} is newer than the newest release {RELEASE}: nothing to update"

pytestmark = pytest.mark.skipif(not (fake_github.available() and shutil.which("ssh-keygen")),
                                reason="needs the openssl and ssh-keygen commands")


def system_prefix():
    """A folder in nobody's home where programs may run (no noexec mount), or None (test_update_github's)."""
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


@pytest.fixture
def github(tmp_path):
    fake = fake_github.FakeGitHub(tmp_path / "tls")
    fake.publish_recorded(recorded(), files(recorded()))
    yield fake
    fake.close()


@pytest.fixture
def system(tmp_path, github):
    """This checkout installed as a system installation (install.sh --system --prefix P, by root here; for every
    user), as the user's 0.1.193 was in /usr/local."""
    if os.geteuid() != 0:
        pytest.skip("a system installation is made by root")
    prefix = system_prefix()
    if prefix is None:
        pytest.skip("no folder outside the homes to install into")
    env = environment(tmp_path, github)
    done = run(["bash", ROOT / "install.sh"], env, "--from", ROOT, "--system", "--prefix", prefix, *APP_OFF)
    assert done[0] == 0, done[2][-3000:]
    yield prefix, env
    subprocess.run(["chmod", "-R", "u+w", str(prefix)], check=False)
    shutil.rmtree(prefix, ignore_errors=True)


def test_a_system_installation_newer_than_the_newest_release_says_so_at_every_entry(system):
    prefix, env = system
    command = [prefix / "bin" / "sandbox-ai"]
    data = prefix / "lib" / "sandbox-ai"
    code, out, err = run(command, env, "update", "--check")
    assert code == 0, out + err
    assert f"installed: {INSTALLED} ({data})" in out and f"available: {RELEASE} " in out
    assert f"result: {NOTHING}" in out, out
    # install.sh's own command of the installation (sandbox-ai-update): its installation, not /usr/local's or yours
    code, out, err = run([prefix / "bin" / "sandbox-ai-update"], env, "--check")
    assert code == 0 and f"installed: {INSTALLED} ({data})" in out, out + err
    assert f"result: {NOTHING}" in out
    # the update itself: nothing to do, said plainly - not a failure, not "an old release served again?"
    code, out, err = run(command, env, "update", *APP_OFF)
    assert code == 0, out + err
    assert NOTHING in err and "served again" not in err and "--allow-downgrade" in err
    code, out, err = run(command, env, "update", "--install")
    assert code == 0 and NOTHING in out + err, out + err
    code, out, err = run(command, env, "update", "mode")
    assert f"last check: " in out and f"{INSTALLED} is newer than the newest release {RELEASE}: nothing to update" \
        in out, out
    status = json.loads(run(command, env, "update", "mode", "--json")[1])
    assert status["result"]["newer"] is False and status["installed"] == INSTALLED
    assert run(command, env, "--version")[1].strip() == f"sandbox-ai {INSTALLED}", "nothing changed"


def test_an_explicit_older_version_is_still_refused_as_a_downgrade(system):
    prefix, env = system
    code, out, err = run([prefix / "bin" / "sandbox-ai"], env, "update", "--version", RELEASE, *APP_OFF)
    assert code == 1 and f"sandbox-ai {RELEASE} is older than the installed {INSTALLED}" in err, out + err
    assert "--allow-downgrade installs it anyway" in err


def test_a_checkout_newer_than_the_newest_release_says_so(tmp_path, github):
    env = environment(tmp_path, github)
    code, out, err = run([ROOT / "sandbox-ai"], env, "update", "--check")
    assert code == 0 and f"result: {NOTHING}" in out, out + err


def test_a_package_newer_than_the_newest_release_says_nothing_to_update(tmp_path, monkeypatch):
    """The .pyz, the AppImage, the macOS app, pip: `sandbox-ai update` of a copy newer than the newest release ended
    with exit 1 and "an old release served again?" (package_update.refuse_older); now it says it plainly, status 0."""
    updates = kit.load(ROOT / "sandbox" / "updates.py", "updates_for_paths")
    packages = updates.packages()
    sums, signature = (RECORDED / "SHA256SUMS").read_bytes(), (RECORDED / "SHA256SUMS.sig").read_bytes()
    links = {asset["name"]: (asset["browser_download_url"], "github.com") for asset in recorded()["assets"]}
    monkeypatch.setattr(packages, "release", lambda source, version=None, environ=None: {
        "version": RELEASE, "tag": f"v{RELEASE}", "assets": links, "source": source})
    monkeypatch.setattr(packages, "fetch", lambda url, host, accept=None, limit=None, environ=None:
                        sums if url.endswith("/SHA256SUMS") else signature)
    file = tmp_path / "bin" / "sandbox-ai.pyz"
    file.parent.mkdir()
    file.write_bytes(b"the package")
    monkeypatch.setenv("SANDBOX_AI_PACKAGE", str(file))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    info = {"kind": "package", "tree": str(ROOT), "data": None, "prefix": None, "package": "pyz"}
    with pytest.raises(packages.UpToDate) as found:
        packages.fetch_release(info, "https://github.com/c0decave/sandbox-ai")
    assert str(found.value).startswith(NOTHING) and "served again" not in str(found.value)
    said = []
    assert updates.install_now(info, source="https://github.com/c0decave/sandbox-ai", busy=lambda: [],
                               out=said.append) is None
    assert said and said[0].startswith(NOTHING)
    with pytest.raises(packages.UpdateError, match=f"sandbox-ai {RELEASE} is older than the installed"):
        packages.fetch_release(info, "https://github.com/c0decave/sandbox-ai", version=RELEASE)


def test_the_last_check_in_words_says_newer_than_the_newest_release():
    updates = kit.load(ROOT / "sandbox" / "updates.py", "updates_for_paths_words")
    assert updates.result_text({"installed": "0.1.193", "available": "0.1.188", "signature": "good",
                                "newer": False}) == \
        "0.1.193 is newer than the newest release 0.1.188: nothing to update"
    assert updates.result_text({"installed": "0.1.188", "available": "0.1.188", "signature": "good",
                                "newer": False}) == "up to date (0.1.188; the newest release is 0.1.188)"


def test_install_sh_of_a_users_installation_checks_that_installation_also_as_root(tmp_path, github):
    """~/.local/bin/sandbox-ai-update (the installation's own install.sh) run by root - root's own installation, or
    sudo -E - looked at /usr/local's system installation ("installed: none"); it acts on the installation it belongs
    to."""
    env = environment(tmp_path, github)
    done = run(["bash", ROOT / "install.sh"], env, "--from", ROOT, "--user", *APP_OFF)
    assert done[0] == 0, done[2][-2000:]
    data = tmp_path / "home" / ".local" / "share" / "sandbox-ai"
    code, out, err = run([tmp_path / "home" / ".local" / "bin" / "sandbox-ai-update"], env, "--check")
    assert code == 0 and f"installed: {INSTALLED} ({data})" in out, out + err
    assert f"result: {NOTHING}" in out


def test_a_reinstall_that_stops_half_way_leaves_the_installed_version_whole(tmp_path, github):
    """--force (the same version again) removed the installed tree first and moved the new one in after: stopped in
    between (a full disk, a refused rename, Ctrl-C) the installation's current pointed at nothing - every command
    gone. Now the new tree is put beside it and swapped in by renames; stopped half way, the old one is whole."""
    env = environment(tmp_path, github)
    done = run(["bash", ROOT / "install.sh"], env, "--from", ROOT, "--user", *APP_OFF)
    assert done[0] == 0, done[2][-2000:]
    command = [tmp_path / "home" / ".local" / "bin" / "sandbox-ai"]
    shim = tmp_path / "shim"
    shim.mkdir()
    (shim / "mv").write_text('#!/bin/sh\ncase "$2" in */versions/*) echo "mv: No space left on device" >&2; exit 1 ;; '
                             'esac\nexec /bin/mv "$@"\n')
    (shim / "mv").chmod(0o755)
    stopped = dict(env, PATH=f"{shim}:{env['PATH']}")
    code, out, err = run(["bash", ROOT / "install.sh"], stopped, "--from", ROOT, "--user", "--force", *APP_OFF)
    assert code != 0, out + err
    assert run(command, env, "--version")[1].strip() == f"sandbox-ai {INSTALLED}", "the installed version is whole"
    code, out, err = run(["bash", ROOT / "install.sh"], env, "--from", ROOT, "--user", "--force", *APP_OFF)
    assert code == 0, err[-2000:]
    versions = tmp_path / "home" / ".local" / "share" / "sandbox-ai" / "versions"
    assert sorted(p.name for p in versions.iterdir()) == [INSTALLED], "nothing half-made is left"


def test_a_release_the_source_never_published_is_named(monkeypatch):
    """A pip installation's rollback fetches the release before again: one that source never published (the copy came
    from a checkout or another source) said "HTTP 404 (private repository? set SANDBOX_AI_TOKEN)". Now it says that
    the source has no such release."""
    packages = kit.load(ROOT / "sandbox" / "package_update.py", "package_update_for_paths")

    def missing(url, host, accept=None, limit=None, environ=None):
        raise packages.UpdateError(f"{url}: HTTP 404 (private repository? set SANDBOX_AI_TOKEN)")
    monkeypatch.setattr(packages, "fetch", missing)
    with pytest.raises(packages.UpdateError) as found:
        packages.release("https://github.com/c0decave/sandbox-ai", "0.1.180", environ={})
    assert str(found.value) == ("https://github.com/c0decave/sandbox-ai has no release 0.1.180 (HTTP 404: never "
                                "published there, or removed; a private repository needs SANDBOX_AI_TOKEN)")
