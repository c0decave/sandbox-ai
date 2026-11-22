"""The logic of the macOS live check (scripts/live_gaps.py's macOS part, tests/test_live_macos.py,
tests/local_releases.py), on Linux with fakes: the disk images given (a .dmg or a downloaded release's folder, a
signed release or a local build), the app given, the releases served on 127.0.0.1 as the updater reads them, a
whole run on a Mac with stand-ins (what runs, what is skipped and why, the report), and the update check itself with
stand-ins for hdiutil, ditto and codesign (tests/release_kit.py): from a signed release and from local builds, and
- with SANDBOX_AI_TEST_APP_PYTHON, a Python with PySide6 - the app's Install now… and Roll back…. Never a proof of
the Mac's: that is scripts/live_check.sh on one (docs/LIVE_CHECK.md)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

from local_releases import LocalReleases  # noqa: E402
import release_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_gaps_macos_logic", ROOT / "scripts" / "live_gaps.py")
LG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LG)
pu_spec = importlib.util.spec_from_file_location("package_update_macos_logic", ROOT / "sandbox" / "package_update.py")
PU = importlib.util.module_from_spec(pu_spec)
pu_spec.loader.exec_module(PU)


def options(**more):
    return SimpleNamespace(**{"dmg_old": None, "dmg_new": None, "app": None, **more})


def release_folder(folder, version, dmg_bytes=b"disk image", signed=True, key=None, arch="arm64"):
    """FOLDER as a downloaded release: sandbox-ai-ARCH.dmg, and when SIGNED its SHA256SUMS (with the bundle's
    line) and SHA256SUMS.sig."""
    folder.mkdir(parents=True, exist_ok=True)
    dmg = folder / kit.assets.dmg(arch)
    dmg.write_bytes(dmg_bytes)
    if signed:
        sums = f"{kit.sha256(dmg)}  {dmg.name}\n{'1' * 64}  {kit.assets.bundle(version)}\n".encode()
        (folder / "SHA256SUMS").write_bytes(sums)
        (folder / "SHA256SUMS.sig").write_bytes(kit.rk.sign(sums, key.path) if key else b"signature")
    return dmg


# --- the inputs ------------------------------------------------------------------------------------------------------

def test_a_disk_image_is_a_dmg_or_the_folder_of_a_release(tmp_path):
    dmg = release_folder(tmp_path / "r", "0.1.154")
    assert LG.dmg_file(dmg, "arm64") == (dmg.resolve(), None)
    assert LG.dmg_file(tmp_path / "r", "arm64") == (dmg.resolve(), None)  # this Mac's, by its name in a release
    (tmp_path / "r" / kit.assets.dmg("x86_64")).write_bytes(b"other")
    assert LG.dmg_file(tmp_path / "r", "x86_64")[0].name == "sandbox-ai-x86_64.dmg"
    (tmp_path / "one").mkdir()
    (tmp_path / "one" / "mine.dmg").write_bytes(b"a local build")
    assert LG.dmg_file(tmp_path / "one", "arm64")[0].name == "mine.dmg"  # the one there
    (tmp_path / "none").mkdir()
    assert "no sandbox-ai-arm64.dmg in it" in LG.dmg_file(tmp_path / "none", "arm64")[1]
    assert "neither a .dmg nor a folder" in LG.dmg_file(tmp_path / "missing.dmg", "arm64")[1]
    (tmp_path / "two").mkdir()
    for name in ("a.dmg", "b.dmg"):
        (tmp_path / "two" / name).write_bytes(b"x")
    assert "(it has a.dmg, b.dmg)" in LG.dmg_file(tmp_path / "two", "arm64")[1]


def test_a_signed_release_lists_the_disk_image_with_its_checksum(tmp_path):
    assert LG.signed_release(release_folder(tmp_path / "signed", "0.1.155"))
    assert not LG.signed_release(release_folder(tmp_path / "local", "0.1.155", signed=False))
    other = release_folder(tmp_path / "changed", "0.1.155")
    other.write_bytes(b"not the image the release lists")
    assert not LG.signed_release(other)
    unsigned = release_folder(tmp_path / "nosig", "0.1.155")
    (unsigned.parent / "SHA256SUMS.sig").unlink()
    assert not LG.signed_release(unsigned)


def test_the_disk_images_and_the_app_for_the_checks(tmp_path):
    old = release_folder(tmp_path / "old", "0.1.154")
    new = release_folder(tmp_path / "new", "0.1.155", dmg_bytes=b"newer", signed=False)
    env = {}
    found = LG.prepare_dmgs(options(dmg_old=str(old.parent), dmg_new=str(new)), env, machine="arm64")
    assert found.ok and found.detail == ("--dmg-old sandbox-ai-arm64.dmg (a signed release); --dmg-new "
                                         "sandbox-ai-arm64.dmg (a local build)")
    assert env == {"SAI_LIVE_DMG_OLD": str(old.resolve()), "SAI_LIVE_DMG_NEW": str(new.resolve())}
    assert LG.prepare_app(options(), env) == LG.Found(True, "the app in sandbox-ai-arm64.dmg (--dmg-new), put into "
                                                            "the check's folder")
    assert not LG.prepare_dmgs(options(dmg_old=str(old)), {}).ok
    assert "the same disk image" in LG.prepare_dmgs(options(dmg_old=str(old), dmg_new=str(old)), {}).detail
    assert LG.prepare_dmgs(options(dmg_old=str(tmp_path), dmg_new=str(new)), {}, "arm64").detail.startswith(
        "--dmg-old ")
    app = tmp_path / "Applications" / "sandbox-ai.app"
    assert "not a sandbox-ai.app" in LG.prepare_app(options(app=str(app)), {}).detail
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/sandbox-ai").write_text("#!/bin/sh\n")
    env = {}
    assert LG.prepare_app(options(app=str(app)), env).ok and env["SAI_LIVE_APP"] == str(app.resolve())
    assert LG.prepare_app(options(), {}).detail == "give --app PATH (a sandbox-ai.app) or --dmg-new DMG"


# --- the releases served on 127.0.0.1 ---------------------------------------------------------------------------------

def test_the_updater_reads_the_releases_served_here(tmp_path):
    image = tmp_path / "sandbox-ai-arm64.dmg"
    image.write_bytes(b"the image")
    with LocalReleases() as releases:
        releases.publish("0.1.154", {"SHA256SUMS": b"sums 154", kit.assets.bundle("0.1.154"): None})
        releases.publish("0.1.155", {"SHA256SUMS": b"sums 155", "sandbox-ai-arm64.dmg": image,
                                     kit.assets.bundle("0.1.155"): None})
        newest = PU.release(releases.source, environ={})
        assert newest["version"] == "0.1.155" and newest["tag"] == "v0.1.155"
        assert set(newest["assets"]) == {"SHA256SUMS", "sandbox-ai-arm64.dmg", kit.assets.bundle("0.1.155")}
        assert PU.release(releases.source, "0.1.154", environ={})["version"] == "0.1.154"
        url, host = newest["assets"]["sandbox-ai-arm64.dmg"]
        assert url.startswith("http://127.0.0.1:") and host == url.split("/")[2]
        assert PU.fetch(url, host, environ={}) == b"the image" and releases.downloads == ["sandbox-ai-arm64.dmg"]
        with pytest.raises(PU.UpdateError, match="HTTP 404"):  # a bundle the app never fetches: listed, not served
            PU.fetch(*newest["assets"][kit.assets.bundle("0.1.155")], environ={})
        listing = json.loads(PU.fetch(f"{releases.base}/api/v1/repos/live/sandbox-ai/releases", "x", environ={}))
        assert [entry["tag_name"] for entry in listing] == ["v0.1.155", "v0.1.154"]


# --- a whole run on a Mac, with stand-ins -----------------------------------------------------------------------------

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" tests="1">{}</testsuite></testsuites>"""


def test_a_whole_run_on_a_mac_with_stand_ins(tmp_path, monkeypatch):
    """main() on a Mac (stand-ins for sw_vers, podman machine and pytest): the macOS checks run with the inputs the
    script sets (the disk images, the app), the Linux checks are skipped saying they need Linux, nothing is said
    to leave the Mac, the report says the macOS version."""
    home = tmp_path / "home" / "ada"
    home.mkdir(parents=True)
    old = release_folder(tmp_path / "old", "0.1.154")
    new = release_folder(tmp_path / "new", "0.1.155", dmg_bytes=b"newer")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(LG.os, "geteuid", lambda: 501)
    monkeypatch.setattr(LG.platform, "machine", lambda: "arm64")

    def runner(command, timeout=60, env=None, cwd=None, input=None):
        if command[0] == "sw_vers":
            return 0, "15.6.1\n", ""
        if command[0].endswith("bash"):
            return 0, "5\n", ""
        if command[:3] == ["podman", "machine", "inspect"]:
            return 0, json.dumps([{"Name": "podman-machine-default", "State": "running"}]), ""
        if command[:2] == ["podman", "info"]:
            return 0, json.dumps({"version": {"Version": "5.6.1"}, "host": {"security": {"rootless": True}}}), ""
        if command[0] == "git":
            return 0, "git version 2.50.1\n" if command[1] == "--version" else "abc1234\n", ""
        return 0, "sandbox-ai 0.1.155\n", ""
    prerequisites = LG.prerequisites
    monkeypatch.setattr(LG, "prerequisites", lambda env, args: prerequisites(
        env, args, system="Darwin", runner=runner, user="ada", uid=501, which=lambda tool: f"/usr/bin/{tool}"))
    real = LG.load_live_check()
    monkeypatch.setattr(real, "inside_a_container", lambda: False)
    monkeypatch.setattr(real, "pytest_python", lambda directory: sys.executable)
    monkeypatch.setattr(LG, "load_live_check", lambda: real)
    seen = {}

    def fake_pytest(command, log, timeout, env, cwd):
        junit = Path(next(part for part in command if part.startswith("--junitxml="))[len("--junitxml="):])
        seen[junit.stem] = {"command": command, "env": env}
        junit.write_text(JUNIT.format(f'<testcase classname="tests.test_live_macos" name="test_{junit.stem}" '
                                      'time="3"/>'))
        Path(log).write_text("1 passed\n")
        return 0, 3.0
    monkeypatch.setattr(LG, "run_logged", fake_pytest)
    lines = []
    assert LG.main(["--out", str(tmp_path / "out"), "--work-dir", str(tmp_path), "--dmg-old", str(old.parent),
                    "--dmg-new", str(new.parent)], out=lines.append, stdin=io.StringIO("")) == 0
    report = json.loads(next((tmp_path / "out").glob("REPORT-*.json")).read_text())
    outcomes = {check["name"]: check["outcome"] for check in report["checks"]}
    assert {name: outcomes[name] for name in ("macos-doctor", "macos-devices", "macos-app", "macos-update")} == \
        dict.fromkeys(("macos-doctor", "macos-devices", "macos-app", "macos-update"), "passed")
    for name in ("release-gates", "firejail-hides-control", "kits-attach", "devices-scan", "devices-live"):
        assert outcomes[name] == "skipped", name
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["devices-scan"]["missing"] == ["linux: Darwin: this check needs Linux (on a Mac the macos-* "
                                                 "checks run)"]
    update = seen["macos-update"]
    assert update["env"]["SAI_LIVE_DMG_OLD"] == str(old.resolve()) and update["env"]["SAI_LIVE_DMG_NEW"] == \
        str(new.resolve())
    assert "tests/test_live_macos.py::test_live_macos_dmg_update_and_rollback" in update["command"] and \
        "tests/test_live_macos.py::test_live_macos_app_install_now_and_roll_back" in update["command"]
    assert "SAI_LIVE_APP" not in seen["macos-app"]["env"], "the app of --dmg-new: put into the check's own folder"
    assert report["facts"]["macos"] == "15.6.1" and report["facts"]["distribution"] == "macOS"
    said = "\n".join(lines)
    assert "ok    macos: macOS 15.6.1 (arm64), hdiutil, ditto, codesign, bash 5" in said
    assert "ok    podman-machine:" in said
    assert "ok    dmgs: --dmg-old sandbox-ai-arm64.dmg (a signed release)" in said
    # Only update-live leaves the Mac (GitHub's releases, read-only: the macOS app updates from there too), and it is
    # said before anything runs (2026-11-22, when update-live came; before it nothing left a Mac).
    sends = said.split("What these checks send from this machine:", 1)[1].split("\n\n", 1)[0]
    assert [line.strip().split(":")[0] for line in sends.splitlines() if line.strip().startswith("- ")] == \
        ["- update-live"], sends
    assert "MISS  userns" not in said, "a Mac is not asked for Linux's namespaces"
    text = next((tmp_path / "out").glob("REPORT-*.md")).read_text()
    assert "| passed | macos-update |" in text and str(home) not in text


# --- the update check itself, with stand-ins for hdiutil, ditto and codesign ------------------------------------------

OLD, NEW = kit.later(kit.VERSION, 1), kit.later(kit.VERSION, 2)


@pytest.fixture(scope="module")
def disk_images(tmp_path_factory):
    """Stand-in disk images of this checkout as OLD and NEW (tests/release_kit.py: the real launchers, the tree, a
    Python of this machine), each as a signed release folder (the kit's key, which both apps trust) and alone (a
    local build)."""
    if not shutil.which("ssh-keygen"):
        pytest.skip("needs ssh-keygen")
    base = tmp_path_factory.mktemp("dmgs")
    key = kit.new_key(base / "keys")
    python = os.environ.get("SANDBOX_AI_TEST_APP_PYTHON") or sys.executable
    arch = kit.assets.arch_of("macos-app")
    if arch is None:
        pytest.skip("no macOS app name for this machine")
    found = {}
    for version in (OLD, NEW):
        tree = kit.tree_copy(base / f"tree-{version}", version, [key])
        dist = kit.make_dist(tree, base / f"dist-{version}", "--skip-python")
        bundle = dist / kit.assets.bundle(version)
        signed = base / f"signed-{version}"
        signed.mkdir()
        dmg = kit.fake_dmg(bundle, signed, arch, python=python)
        shutil.copy2(bundle, signed / bundle.name)
        sums = f"{kit.sha256(dmg)}  {dmg.name}\n{kit.sha256(bundle)}  {bundle.name}\n".encode()
        (signed / "SHA256SUMS").write_bytes(sums)
        (signed / "SHA256SUMS.sig").write_bytes(kit.rk.sign(sums, key.path))
        local = base / f"local-{version}"
        local.mkdir()
        shutil.copy2(dmg, local / dmg.name)
        found[version] = SimpleNamespace(signed=signed, local=local)
        kit.remove(tree)
        kit.remove(dist)
    yield found
    kit.remove(base)


def live_test(tmp_path, name, old, new, extra_env=None):
    stubs = kit.hdiutil_stub(tmp_path / "stubs")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_", "SAI_LIVE"))}
    env.update(SAI_LIVE_MACOS_STANDIN="1", SAI_LIVE_DMG_OLD=str(old), SAI_LIVE_DMG_NEW=str(new),
               PATH=f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}", HOME=str(tmp_path / "home"), **(extra_env or {}))
    (tmp_path / "home").mkdir(exist_ok=True)
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-s", "-p", "no:cacheprovider", "-rfEs",
                           "--agent-live", f"--basetemp={tmp_path / 'bt'}", f"tests/test_live_macos.py::{name}"],
                          cwd=ROOT, capture_output=True, text=True, env=env, timeout=1800)
    kit.remove(tmp_path / "bt")
    return done, (stubs / "calls").read_text() if (stubs / "calls").exists() else ""


@pytest.mark.parametrize("kind", ["signed", "local"])
def test_the_update_check_from_a_release_and_from_local_builds(tmp_path, disk_images, kind):
    """test_live_macos_dmg_update_and_rollback with stand-ins: OLD's app put into the test's folder updates to NEW
    from the releases it serves and rolls back - with the release's key (signed release folders) and with the
    check's own (local builds: the old app is told to trust it)."""
    old, new = getattr(disk_images[OLD], kind), getattr(disk_images[NEW], kind)
    done, calls = live_test(tmp_path, "test_live_macos_dmg_update_and_rollback", old, new)
    assert done.returncode == 0 and "1 passed" in done.stdout, (done.stdout + done.stderr)[-4000:]
    assert f"updated {OLD} -> {NEW} and back; {'signed releases' if kind == 'signed' else 'local builds'}" in \
        done.stdout
    assert "hdiutil attach -nobrowse -readonly -noautoopen -mountpoint" in calls and "ditto " in calls
    assert "codesign --verify --deep --strict" in calls


def test_the_update_check_says_what_it_needs(tmp_path, disk_images):
    """Without disk images it is skipped saying what to give; an old app before 0.1.154 is skipped saying why;
    disk images in the wrong order fail saying so."""
    stubs = kit.hdiutil_stub(tmp_path / "stubs")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_", "SAI_LIVE"))}
    env.update(SAI_LIVE_MACOS_STANDIN="1", PATH=f"{stubs}:{env.get('PATH', '/usr/bin:/bin')}")
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", "--agent-live",
                           f"--basetemp={tmp_path / 'bt'}",
                           "tests/test_live_macos.py::test_live_macos_dmg_update_and_rollback"],
                          cwd=ROOT, capture_output=True, text=True, env=env, timeout=600)
    assert "1 skipped" in done.stdout and "--dmg-old DMG --dmg-new DMG" in done.stdout, done.stdout[-2000:]
    done, _ = live_test(tmp_path / "swapped", "test_live_macos_dmg_update_and_rollback", disk_images[NEW].local,
                        disk_images[OLD].local)
    assert "1 failed" in done.stdout and f"--dmg-new ({OLD}) is not newer than --dmg-old ({NEW})" in done.stdout


def test_without_mac_the_macos_tests_skip_saying_so(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("SAI_LIVE")}
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rs", "--agent-live",
                           f"--basetemp={tmp_path / 'bt'}", "tests/test_live_macos.py"],
                          cwd=ROOT, capture_output=True, text=True, env=env, timeout=600)
    assert done.returncode == 0 and "5 skipped" in done.stdout and "it runs on a Mac" in done.stdout, \
        done.stdout[-2000:]


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_TEST_APP_PYTHON"),
                    reason="SANDBOX_AI_TEST_APP_PYTHON: a Python with PySide6, the stand-in app's")
def test_the_apps_install_now_and_roll_back_check(tmp_path, disk_images):
    """test_live_macos_app_install_now_and_roll_back with stand-ins: the old app's own Settings > Updates (its code
    on a Python with PySide6, offscreen) installs NEW with Install now… and goes back with Roll back…."""
    done, calls = live_test(tmp_path, "test_live_macos_app_install_now_and_roll_back", disk_images[OLD].local,
                            disk_images[NEW].local)
    assert done.returncode == 0 and "1 passed" in done.stdout, (done.stdout + done.stderr)[-4000:]
    assert "ditto " in calls


@pytest.mark.skipif(not os.environ.get("SANDBOX_AI_TEST_APP_PYTHON"),
                    reason="SANDBOX_AI_TEST_APP_PYTHON: a Python with PySide6, the stand-in app's")
def test_the_app_check(tmp_path, disk_images):
    """test_live_macos_app_starts with stand-ins: the app of the new disk image, put into the test's folder - its
    command line's version, doctor's first line (a package: macos-app), sandbox-ai-app --version and --self-test."""
    done, calls = live_test(tmp_path, "test_live_macos_app_starts", disk_images[OLD].local, disk_images[NEW].local)
    assert done.returncode == 0 and "1 passed" in done.stdout, (done.stdout + done.stderr)[-4000:]
    assert "codesign --verify --deep --strict" in calls
