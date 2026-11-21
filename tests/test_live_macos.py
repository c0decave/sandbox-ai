"""The macOS checks of scripts/live_check.sh (docs/LIVE_CHECK.md, "On a Mac"): what only a real Mac shows, where the
other tests have stand-ins - doctor's macOS set, devices there, sandbox-ai.app as CI builds it, and its update from
one disk image to another and back, by its command line and by the app's own Install now… and Roll back….

Opt-in with --agent-live, on macOS only (elsewhere skipped, saying so). Inputs from the script (a test without its
input is skipped and says what it needs): SAI_LIVE_DMG_OLD and SAI_LIVE_DMG_NEW (disk images of sandbox-ai.app;
beside each, a downloaded release's SHA256SUMS and SHA256SUMS.sig - then the release's own key is checked - or
nothing: a local build, whose SHA256SUMS the test signs with a key of its own that the old app it put into its folder
is told to trust), SAI_LIVE_APP (a sandbox-ai.app: only read and started).

Nothing of the user's is touched: sandbox-ai runs with folders of its own (HOME, configuration, state, cache), the
app is put into a folder of the test's (never /Applications) and the releases are served by the test on 127.0.0.1
(SANDBOX_AI_SOURCE): nothing leaves the Mac.

SAI_LIVE_MACOS_STANDIN=1 (only tests/test_live_macos_logic.py sets it, on Linux): the same tests with stand-ins for
hdiutil, ditto and codesign (tests/release_kit.py's) and an app whose Python is a Linux one - a test of these tests'
own logic, never a proof of the Mac's.
"""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
STANDIN = os.environ.get("SAI_LIVE_MACOS_STANDIN") == "1"
sys.path.insert(0, str(Path(__file__).parent))
from local_releases import LocalReleases  # noqa: E402

pytestmark = [pytest.mark.agent_live,
              pytest.mark.skipif(sys.platform != "darwin" and not STANDIN,
                                 reason="a check of macOS: it runs on a Mac (scripts/live_check.sh there)")]


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LG = load("scripts/live_gaps.py", "live_gaps_for_macos")
assets = load("sandbox/release_assets.py", "release_assets_for_macos")
keys = load("sandbox/release_keys.py", "release_keys_for_macos")
DRIVER = Path(__file__).with_name("live_macos_app_driver.py")
FIRST_SELF_UPDATING = (0, 1, 154)  # the first macOS app that replaces itself from a disk image
# doctor's checks of a Linux host, never of a Mac (podman machine's VM has them)
LINUX_ONLY = {"User namespaces", "systemd user session", "pasta", "Delegated UID/GID ranges", "Landlock",
              "Landlock (--untrusted)", "newuidmap, newgidmap"}
VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.M)


# --- helpers ---------------------------------------------------------------------------------------------------------

def environment(tmp_path, **more):
    """sandbox-ai's environment: folders of its own below TMP_PATH/e, no release source or token of the user's."""
    env = LG.isolated_environment(tmp_path / "e", os.environ)
    for name in ("SANDBOX_AI_SOURCE", "SANDBOX_AI_TOKEN", "SANDBOX_AI_HOME"):
        env.pop(name, None)
    env.update(NO_COLOR="1", **more)
    return env


def run(argv, env, timeout=300, **kwargs):
    return subprocess.run([str(word) for word in argv], capture_output=True, text=True, env=env, timeout=timeout,
                          stdin=subprocess.DEVNULL, **kwargs)


def said(done):
    return f"exit {done.returncode}\n{done.stdout[-3000:]}\n{done.stderr[-3000:]}"


def numbers(version):
    return tuple(int(part) for part in re.findall(r"\d+", version)[:3])


def tree_version(app):
    """The version of the sandbox-ai inside the app APP (its tree, as the updater reads it)."""
    text = (Path(app) / "Contents/Resources/sandbox-ai/sandbox/agent_sandbox.py").read_text()
    return VERSION_LINE.search(text)[1]


def cli_of(app):
    return [str(Path(app) / "Contents/MacOS/sandbox-ai")]


def signature(app, env=None):
    """(True, "") when the app's signature holds (codesign --verify --deep --strict), else (False, why)."""
    done = run(["codesign", "--verify", "--deep", "--strict", app], env or os.environ, timeout=300)
    return done.returncode == 0, (done.stderr or done.stdout).strip()[-1000:]


@contextlib.contextmanager
def attached(dmg, folder):
    """The disk image DMG attached read-only (hdiutil) at a folder below FOLDER, detached at the end."""
    mount = Path(tempfile.mkdtemp(prefix="mnt-", dir=folder))
    done = run(["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", mount, dmg], os.environ,
               timeout=600)
    assert done.returncode == 0, f"hdiutil attach {Path(dmg).name}: {said(done)}"
    try:
        yield mount
    finally:
        if run(["hdiutil", "detach", "-quiet", mount], os.environ, timeout=300).returncode != 0:
            run(["hdiutil", "detach", "-force", "-quiet", mount], os.environ, timeout=300)
        with contextlib.suppress(OSError):
            mount.rmdir()


def install(dmg, folder, work):
    """sandbox-ai.app of DMG put into FOLDER as a human does it (dragged out of the disk image: ditto keeps the
    signature): (its path, its version, whether its signature held in the disk image)."""
    folder.mkdir(parents=True, exist_ok=True)
    with attached(dmg, work) as mount:
        source = mount / "sandbox-ai.app"
        assert source.is_dir(), f"{Path(dmg).name} holds no sandbox-ai.app"
        holds, _ = signature(source)
        done = run(["ditto", source, folder / "sandbox-ai.app"], os.environ, timeout=1800)
        assert done.returncode == 0, said(done)
    app = folder / "sandbox-ai.app"
    return app, tree_version(app), holds


def given_dmgs():
    old, new = os.environ.get("SAI_LIVE_DMG_OLD"), os.environ.get("SAI_LIVE_DMG_NEW")
    if not (old and new):
        pytest.skip("give two disk images: scripts/live_check.sh --dmg-old DMG --dmg-new DMG")
    found = []
    for given in (old, new):  # (a .dmg, or a downloaded release's folder - as the script takes them)
        dmg, problem = LG.dmg_file(given)
        assert dmg is not None, problem
        found.append(dmg)
    return found


def dmg_version(dmg, work):
    with attached(dmg, work) as mount:
        return tree_version(mount / "sandbox-ai.app")


def new_key(folder):
    """A throwaway ed25519 key of the test's: (private key path, its allowed-signers line)."""
    folder.mkdir(parents=True, exist_ok=True)
    done = run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "sandbox-ai-live-check", "-f", folder / "key"],
               os.environ, timeout=60)
    assert done.returncode == 0, said(done)
    kind, key = (folder / "key.pub").read_text().split()[:2]
    return folder / "key", keys.line(kind, key)


def release_files(dmg, version, key, machine=None):
    """{name: file} of the release VERSION served for DMG: a downloaded release's own SHA256SUMS and signature (its
    bundle listed: served when beside it), else SHA256SUMS of the disk image (and the bundle's name, which the
    updater wants listed; never downloaded by the app) signed with KEY."""
    name = assets.asset_for("macos-app", version, machine)
    assert name, f"no macOS app is built for {platform.machine()}"
    if LG.signed_release(dmg):
        sums = (dmg.parent / "SHA256SUMS").read_bytes()
        listed = assets.checksums(sums.decode(errors="replace"))
        assert dmg.name == name, f"{dmg} is not this Mac's disk image {name}"
        assert assets.bundle_version(listed) == version, \
            f"the SHA256SUMS beside {dmg.name} is of {assets.bundle_version(listed)}, its app is {version}"
        bundle = assets.bundle(version)
        return {name: dmg, "SHA256SUMS": sums, "SHA256SUMS.sig": (dmg.parent / "SHA256SUMS.sig").read_bytes(),
                bundle: dmg.parent / bundle if (dmg.parent / bundle).is_file() else None}
    sums = f"{LG.file_sha256(dmg)}  {name}\n{'0' * 64}  {assets.bundle(version)}\n".encode()
    return {name: dmg, "SHA256SUMS": sums, "SHA256SUMS.sig": keys.sign(sums, key), assets.bundle(version): None}


def trust(app, line):
    """The old app trusts the test's key (local builds only: their SHA256SUMS is signed by it)."""
    signers = Path(app) / "Contents/Resources/sandbox-ai/sandbox/release-signers"
    with open(signers, "a") as stream:
        stream.write(f"\n{line}\n")


def prepared(tmp_path, releases):
    """The old app put into TMP_PATH/Applications, its release published (the new one ready to be): (app, old
    version, new version, publish_new, signed: both releases signed by the release's key, the old app's signature
    held, the new app's)."""
    old_dmg, new_dmg = given_dmgs()
    work = tmp_path / "w"
    work.mkdir()
    app, old, old_holds = install(old_dmg, tmp_path / "Applications", work)
    new = dmg_version(new_dmg, work)
    if numbers(old) < FIRST_SELF_UPDATING:
        pytest.skip(f"--dmg-old is sandbox-ai {old}: the macOS app replaces itself from 0.1.154 on")
    assert numbers(new) > numbers(old), f"--dmg-new ({new}) is not newer than --dmg-old ({old})"
    with attached(new_dmg, work) as mount:
        new_holds, _ = signature(mount / "sandbox-ai.app")
    signed = LG.signed_release(old_dmg) and LG.signed_release(new_dmg)
    key = None
    if not signed:
        key, line = new_key(work / "key")
        trust(app, line)
    releases.publish(old, release_files(old_dmg, old, key))
    return SimpleRun(app, old, new, lambda: releases.publish(new, release_files(new_dmg, new, key)), signed,
                     old_holds, new_holds)


class SimpleRun:
    def __init__(self, app, old, new, publish_new, signed, old_holds, new_holds):
        self.app, self.old, self.new, self.publish_new = app, old, new, publish_new
        self.signed, self.old_holds, self.new_holds = signed, old_holds, new_holds


def doctor(cli, env):
    done = run([*cli, "doctor", "--json"], env, timeout=300)
    assert done.returncode in (0, 1), said(done)  # (1: some check failed - what the checks below look at)
    return json.loads(done.stdout)


def machine_verdict_matches(entries, env):
    """doctor's "podman machine" says what `podman machine inspect` says."""
    titles = {entry["check"] for entry in entries}
    assert "podman machine" in titles, titles
    assert not titles & LINUX_ONLY, f"checks of a Linux host on a Mac: {sorted(titles & LINUX_ONLY)}"
    for entry in entries:
        assert entry["status"] in ("ok", "warn", "fail", "skip") and isinstance(entry["detail"], str), entry
    verdict = next(entry for entry in entries if entry["check"] == "podman machine")
    found = shutil.which("podman", path=env.get("PATH"))
    inspect = run(["podman", "machine", "inspect"], env, timeout=120) if found else None
    try:
        machines = json.loads(inspect.stdout) if inspect is not None and inspect.returncode == 0 else []
    except ValueError:
        machines = []
    machine = machines[0] if isinstance(machines, list) and machines and isinstance(machines[0], dict) else None
    if machine is None:
        assert verdict["status"] == "fail" and verdict["hint"], verdict
    elif machine.get("State") != "running":
        assert verdict["status"] == "fail" and "podman machine start" in verdict["hint"], verdict
    elif machine.get("Rootful"):
        assert verdict["status"] == "fail" and "rootful" in verdict["detail"], verdict
    else:
        assert verdict["status"] in ("ok", "warn") or "not shared" in verdict["detail"], verdict
    return verdict


# --- the checks ------------------------------------------------------------------------------------------------------

def test_live_macos_doctor(tmp_path):
    """sandbox-ai doctor on this Mac (the checkout's, and the app's command line when an app is given): the macOS
    set - podman machine, none of a Linux host's checks -, every check with a state, and podman machine's verdict
    as `podman machine inspect` says it. What it found is printed."""
    env = environment(tmp_path)
    clis = [[str(SAI)]]
    if os.environ.get("SAI_LIVE_APP"):
        clis.append(cli_of(os.environ["SAI_LIVE_APP"]))
    for cli in clis:
        entries = doctor(cli, env)
        verdict = machine_verdict_matches(entries, env)
        print(f"{cli[0]}: podman machine {verdict['status']}: {verdict['detail']}; "
              + ", ".join(f"{e['check']} {e['status']}" for e in entries))


def test_live_macos_devices(tmp_path):
    """sandbox-ai devices scan on this Mac: JSON with the documented fields, every node it lists exists, every
    refusal has a reason, nothing offered for --device (a start here refuses every --device: the sandbox runs in
    podman machine's Linux VM); the text form works; a start with --device is refused, saying why."""
    env = environment(tmp_path)
    done = run([SAI, "devices", "scan", "--json"], env, timeout=120)
    assert done.returncode == 0, said(done)
    scan = json.loads(done.stdout)
    assert isinstance(scan["devices"], list) and isinstance(scan["notes"], list)
    for entry in scan["devices"]:
        assert {"kind", "value", "label", "nodes", "option", "refused"} <= set(entry), entry
        for node in entry["nodes"]:
            assert os.path.exists(node), (node, entry)
        assert entry["refused"] is None or (isinstance(entry["refused"], str) and entry["refused"]), entry
    offered = [entry["value"] for entry in scan["devices"] if entry["option"] == "--device" and not entry["refused"]]
    assert offered == [], f"offered for --device, which a start on macOS refuses: {offered}"
    text = run([SAI, "devices", "scan"], env, timeout=120)
    assert text.returncode == 0, said(text)
    start = run([SAI, "codex", "--dry-run", "--device", "/dev/null"], env, timeout=300)
    assert start.returncode != 0 and "on macOS the sandbox runs in Podman's Linux VM" in start.stderr, said(start)
    print("devices scan:", json.dumps({e["kind"]: e["refused"] or "offered" for e in scan["devices"]}),
          "notes:", scan["notes"])


def test_live_macos_app_starts(tmp_path):
    """sandbox-ai.app (SAI_LIVE_APP, else the one in the new disk image, put into a folder of the test's): its
    signature holds, its command line says its version and that it is the macOS app (doctor's first line: a
    package, macos-app), `sandbox-ai-app --version` names the sandbox-ai inside it, and the app builds its window
    against it and quits (--self-test, on this Mac's display)."""
    env = environment(tmp_path)
    if os.environ.get("SAI_LIVE_APP"):
        app = Path(os.environ["SAI_LIVE_APP"])
    elif os.environ.get("SAI_LIVE_DMG_NEW"):
        work = tmp_path / "w"
        work.mkdir()
        dmg, problem = LG.dmg_file(os.environ["SAI_LIVE_DMG_NEW"])
        assert dmg is not None, problem
        app, _, _ = install(dmg, tmp_path / "Applications", work)
    else:
        pytest.skip("give an app: scripts/live_check.sh --app PATH or --dmg-new DMG")
    holds, why = signature(app, env)
    assert holds, f"codesign --verify --deep --strict {app.name}: {why}"
    version = tree_version(app)
    done = run([*cli_of(app), "--version"], env)
    assert done.returncode == 0 and done.stdout.strip() == f"sandbox-ai {version}", said(done)
    first = doctor(cli_of(app), env)[0]
    assert first.get("id") == "sandbox-ai-version" and first.get("kind") == "package" and \
        first.get("package") == "macos-app" and first.get("version") == version, first
    gui = Path(app) / "Contents/MacOS/sandbox-ai-app"
    done = run([gui, "--version"], env, timeout=120)
    inside = str(Path(app) / "Contents/Resources/sandbox-ai")
    assert done.returncode == 0 and f"sandbox-ai {version} from {inside}" in done.stdout, said(done)
    started = run([gui, "--self-test"], {**env, **({"QT_QPA_PLATFORM": "offscreen"} if STANDIN else {})},
                  timeout=600)
    assert started.returncode == 0, said(started)


def test_live_macos_dmg_update_and_rollback(tmp_path):
    """The old disk image's app, put into a folder of the test's, updates itself with `sandbox-ai update` from the
    release of the new disk image served on 127.0.0.1: up to date while only its own release is there, then the
    new one found (signature good), downloaded (the disk image, SHA256SUMS and its signature - nothing else),
    installed (hdiutil, ditto: the new app's signature holds; nothing left beside it), and `update --rollback`
    brings the old one back."""
    with LocalReleases() as releases:
        setup = prepared(tmp_path, releases)
        app, old, new, cli = setup.app, setup.old, setup.new, cli_of(setup.app)
        env = environment(tmp_path, SANDBOX_AI_SOURCE=releases.source)
        done = run([*cli, "--version"], env)
        assert done.stdout.strip() == f"sandbox-ai {old}", said(done)
        quiet = run([*cli, "update"], env, timeout=600)
        assert quiet.returncode == 0 and f"sandbox-ai {old} is already installed and up to date" in \
            quiet.stdout + quiet.stderr, said(quiet)
        setup.publish_new()
        check = run([*cli, "update", "--check"], env, timeout=600)
        assert check.returncode == 0 and f"available: {new}" in check.stdout and "signature: good" in check.stdout, \
            said(check)
        releases.downloads.clear()
        update = run([*cli, "update"], env, timeout=1900)
        assert update.returncode == 0 and f"installed {new} (was {old})" in update.stdout, said(update)
        name = assets.asset_for("macos-app", new)
        assert sorted(releases.downloads) == sorted([name, "SHA256SUMS", "SHA256SUMS.sig"]), releases.downloads
        done = run([*cli, "--version"], env)
        assert done.stdout.strip() == f"sandbox-ai {new}", said(done)
        assert sorted(p.name for p in app.parent.iterdir()) == ["sandbox-ai.app"], "nothing is left beside the app"
        assert (app / "Contents/Resources/sandbox-ai/PACKAGING").read_text().strip() == "macos-app"
        if setup.new_holds:
            holds, why = signature(app, env)
            assert holds, f"the new app's signature after the update (ditto): {why}"
        status = json.loads(run([*cli, "update", "mode", "--json"], env).stdout)
        assert status["installed"] == new and status["rollback"] == old, status
        back = run([*cli, "update", "--rollback"], env, timeout=600)
        assert back.returncode == 0 and f"back to {old} (was {new})" in back.stdout, said(back)
        done = run([*cli, "--version"], env)
        assert done.stdout.strip() == f"sandbox-ai {old}", said(done)
        if setup.signed and setup.old_holds:  # (a local build's old app was told to trust the test's key: changed)
            holds, why = signature(app, env)
            assert holds, f"the old app's signature after the rollback: {why}"
        status = json.loads(run([*cli, "update", "mode", "--json"], env).stdout)
        assert status["installed"] == old and status["rollback"] == new, status
        print(f"updated {old} -> {new} and back; {'signed releases' if setup.signed else 'local builds'}")


def test_live_macos_app_install_now_and_roll_back(tmp_path):
    """The old app's own Settings > Updates (its code, its Python and PySide6 - tests/live_macos_app_driver.py):
    Check now finds the new release, Install now… (answered yes) installs it, Roll back… brings the old one back;
    the command line says so after each."""
    with LocalReleases() as releases:
        setup = prepared(tmp_path, releases)
        setup.publish_new()
        app, old, new = setup.app, setup.old, setup.new
        resources = app / "Contents/Resources"
        env = environment(tmp_path, SANDBOX_AI_SOURCE=releases.source, QT_QPA_PLATFORM="offscreen",
                          SANDBOX_AI_HOME=str(resources / "sandbox-ai"), SANDBOX_AI_PACKAGE=str(app),
                          SANDBOX_AI_PACKAGE_COPY=str(resources / "sandbox-ai"),
                          SANDBOX_AI_APP_PACKAGE_LAUNCHER=str(app / "Contents/MacOS/sandbox-ai-app"))
        env["PATH"] = env.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:/opt/podman/bin"
        python = resources / "python/bin/python3"
        done = run([python, "-I", "-B", DRIVER, resources / "pyside", resources / "sandbox-ai/projects/sandbox-ai-app",
                    new, old], env, timeout=2700)
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        result = json.loads(lines[-1]) if lines else {}
        assert result.get("step") == "end" and result.get("problem") is None and not result.get("warnings"), \
            said(done)
        assert result["installed"] == [new, old], result
        done = run([*cli_of(app), "--version"], env)
        assert done.stdout.strip() == f"sandbox-ai {old}", said(done)
        assert sorted(p.name for p in app.parent.iterdir()) == ["sandbox-ai.app"], "nothing is left beside the app"
