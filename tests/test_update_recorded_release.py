"""The update check against a recorded copy of the real GitHub release 0.1.188 (published 2026-11-21, the newest
public one when this was written): the answer api.github.com gave for releases/latest, as it came, and the release's
real SHA256SUMS and SHA256SUMS.sig - signed by the release key in sandbox/release-signers. tests/fake_github.py
serves them as https://github.com/c0decave/sandbox-ai to the unchanged code (the commands a user types); nothing
leaves this machine. scripts/live_check.sh's check update-live asks the real GitHub the same questions.

What a user sees: an installation newer than the release says it is up to date (never a downgrade), an older one is
offered the release - signed by the trusted key -, each kind of installation finds its own file in the release, and
a release whose SHA256SUMS was changed, or that has no signature, is never offered.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import fake_github  # noqa: E402
import release_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RECORDED = Path(__file__).resolve().parent / "golden" / "github-release-0.1.188"
RELEASE = "0.1.188"
KEY = "SHA256:4Of0GsID4w0KNilg2VbymVRcyopNJbbq7K1P1qXg+Ug"  # the release key (RELEASING.md, sandbox/release-signers)
OLDER = "0.1.180"

pytestmark = pytest.mark.skipif(not (fake_github.available() and shutil.which("ssh-keygen")),
                                reason="needs the openssl and ssh-keygen commands")
assets = kit.load(ROOT / "sandbox" / "release_assets.py", "release_assets_for_recorded")
keys = kit.load(ROOT / "sandbox" / "release_keys.py", "release_keys_for_recorded")


def recorded():
    return json.loads((RECORDED / "releases-latest.json").read_text())


def files(document, sums=None, signature=True):
    """{name: bytes} behind the recorded release's download links: the real SHA256SUMS (or SUMS) and its signature;
    the other files are never downloaded by a check (their bytes here are a stand-in)."""
    found = {asset["name"]: b"not downloaded by a check" for asset in document["assets"]}
    found["SHA256SUMS"] = sums if sums is not None else (RECORDED / "SHA256SUMS").read_bytes()
    if signature:
        found["SHA256SUMS.sig"] = (RECORDED / "SHA256SUMS.sig").read_bytes()
    else:
        found.pop("SHA256SUMS.sig", None)
    return found


@pytest.fixture
def github(tmp_path):
    fake = fake_github.FakeGitHub(tmp_path / "tls")
    fake.publish_recorded(recorded(), files(recorded()))
    yield fake
    fake.close()
    assert fake.leaked == [] and fake.refused == [], (fake.leaked, fake.refused)


@pytest.fixture(scope="module")
def older(tmp_path_factory):
    """This checkout as sandbox-ai OLDER (release_kit's copy, its version changed there) with the real release keys:
    an installation from before the release."""
    base = tmp_path_factory.mktemp("older")
    tree = kit.tree_copy(base / "tree", OLDER, [])
    shutil.copy2(ROOT / "sandbox" / "release-signers", tree / "sandbox" / "release-signers")
    kit.commit(tree, "the real release keys")
    yield tree
    kit.remove(base)


def environment(tmp_path, github):
    """HOME and the XDG folders below TMP_PATH, notify-send and systemctl stand-ins first on PATH (their calls in
    TMP_PATH/stubs/calls), the fake's proxy and certificate."""
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    for name in ("notify-send", "systemctl", "launchctl", "osascript"):
        (stubs / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{stubs}/calls"\n')
        (stubs / name).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("XDG_", "SANDBOX_AI_", "PYTHON"))}
    env = github.env(env)
    env.update(HOME=str(home), PATH=f"{stubs}:/usr/bin:/bin", TMPDIR=str(tmp_path), NO_COLOR="1",
               DISPLAY=":0")
    return env


def run(command, env, *args):
    done = subprocess.run([str(word) for word in (*command, *args)], capture_output=True, text=True, env=env,
                          timeout=300, stdin=subprocess.DEVNULL)
    return done.returncode, done.stdout, done.stderr


def status(command, env):
    code, out, err = run(command, env, "update", "mode", "--json")
    assert code == 0, err
    return json.loads(out)


# -- the recorded release itself --------------------------------------------------------------------------------------

def test_the_recorded_release_is_signed_by_the_trusted_release_key():
    sums, signature = (RECORDED / "SHA256SUMS").read_bytes(), (RECORDED / "SHA256SUMS.sig").read_bytes()
    trusted = keys.load(ROOT / "sandbox" / "release-signers")
    assert KEY in [keys.fingerprint(key) for _, key in trusted], "the release key is one this checkout trusts"
    assert keys.verify(sums, signature, trusted) == ("good", KEY)
    assert keys.verify(sums.replace(b"45bf05f0", b"00000000"), signature, trusted)[0] != "good"
    assert assets.bundle_version(assets.checksums(sums.decode())) == RELEASE
    document = recorded()
    assert document["tag_name"] == f"v{RELEASE}" and not document["draft"] and not document["prerelease"]


@pytest.mark.parametrize("kind, machine", [
    ("user", None), ("system", None), ("pip", None), ("pyz", None),
    ("appimage", "x86_64"), ("appimage", "aarch64"), ("macos-app", "arm64"), ("macos-app", "x86_64"),
])
def test_each_kind_of_installation_finds_its_file_in_the_release_and_its_signed_list(kind, machine):
    """The file each kind updates from (release_assets.asset_for) is an asset of the real release and listed in its
    signed SHA256SUMS: an update finds it and can check it."""
    name = assets.asset_for(kind, RELEASE, machine)
    listed = assets.checksums((RECORDED / "SHA256SUMS").read_text())
    names = {asset["name"] for asset in recorded()["assets"]}
    assert name in names and name in listed, name
    url = next(a["browser_download_url"] for a in recorded()["assets"] if a["name"] == name)
    assert url == f"https://github.com/c0decave/sandbox-ai/releases/download/v{RELEASE}/{name}"
    assert {"SHA256SUMS", "SHA256SUMS.sig"} <= names


# -- the checks a user runs -------------------------------------------------------------------------------------------

def test_this_checkout_is_newer_than_the_release_and_never_offered_a_downgrade(tmp_path, github):
    """`sandbox-ai update --check` of a git checkout says what the release source has (it changes nothing): a newer
    checkout than the newest release is up to date. (It refused before: "this copy was not installed by install.sh"
    - and the app's check said only "No update information".)"""
    env = environment(tmp_path, github)
    command = [ROOT / "sandbox-ai"]
    code, out, err = run(command, env, "update", "--check")
    assert code == 0, out + err
    installed = kit.VERSION
    assert tuple(map(int, installed.split(".")[:3])) >= tuple(map(int, RELEASE.split("."))), "a checkout after it"
    assert f"installed: {installed} (a git checkout, {ROOT})" in out
    assert f"available: {RELEASE} (https://github.com/c0decave/sandbox-ai)" in out
    assert f"signature: good, by the trusted key {KEY}" in out
    assert "update: git -C" in out
    if installed != RELEASE:
        assert f"older than the installed {installed}" in out
    found = status(command, env)
    assert found["result"]["available"] == RELEASE and found["result"]["signature"] == "good"
    assert found["result"]["signer"] == KEY and found["result"]["newer"] is False
    assert found["result"]["installed"] == installed
    code, out, err = run(command, env, "update", "mode")
    assert f"up to date ({installed}; the newest release is {RELEASE})" in out
    code, out, err = run(command, env, "update")  # (an update of a checkout is git's)
    assert code == 1 and "git checkout" in err


def test_an_older_installation_is_offered_the_release_and_told_once_a_day(tmp_path, github, older):
    env = environment(tmp_path, github)
    command = [older / "sandbox-ai"]
    code, out, err = run(command, env, "update", "--check")
    assert code == 0, out + err
    assert f"installed: {OLDER} (a git checkout, {older})" in out and f"available: {RELEASE} " in out
    assert f"signature: good, by the trusted key {KEY}" in out and "older than" not in out
    found = status(command, env)
    assert found["result"]["newer"] is True and found["notes"] == \
        f"https://github.com/c0decave/sandbox-ai/releases/tag/v{RELEASE}"
    # notify's daily check (the timer's run, or the one a command starts): a desktop notification once per version
    calls = tmp_path / "stubs" / "calls"
    (tmp_path / "home" / ".local" / "state" / "sandbox-ai" / "update.json").unlink()
    code, out, err = run(command, env, "update", "--scheduled")
    assert code == 0 and f"checked: {RELEASE} is available (installed: {OLDER}), signed by {KEY}" in out, out + err
    told = [line for line in calls.read_text().splitlines() if line.startswith("notify-send")]
    assert len(told) == 1 and f"sandbox-ai {RELEASE} is available" in told[0], told
    later = dict(env, SANDBOX_AI_UPDATE_NOW=str(found["last_check"] + 2 * 86400 if found["last_check"] else 0))
    code, out, err = run(command, later, "update", "--scheduled")
    assert code == 0 and "checked:" in out, out + err
    assert len([line for line in calls.read_text().splitlines() if line.startswith("notify-send")]) == 1, \
        "the same version is announced once"


def test_an_installation_as_new_as_the_release_is_told_nothing(tmp_path, github, older):
    """notify's daily check of an installation that has the newest release: no notification."""
    env = environment(tmp_path, github)
    code, out, err = run([ROOT / "sandbox-ai"], env, "update", "--scheduled")
    assert code == 0 and f"checked: up to date ({kit.VERSION}; the newest release is {RELEASE})" in out, out + err
    calls = tmp_path / "stubs" / "calls"
    assert not calls.exists() or "notify-send" not in calls.read_text()


@pytest.mark.parametrize("case", ["changed SHA256SUMS", "no signature"])
def test_a_release_whose_list_was_changed_or_that_is_not_signed_is_never_offered(case, tmp_path, older):
    document = recorded()
    if case == "no signature":
        document["assets"] = [a for a in document["assets"] if a["name"] != "SHA256SUMS.sig"]
        served = files(document, signature=False)
    else:
        served = files(document, sums=(RECORDED / "SHA256SUMS").read_bytes().replace(b"45bf05f0", b"00000000"))
    fake = fake_github.FakeGitHub(tmp_path / "tls")
    try:
        fake.publish_recorded(document, served)
        env = environment(tmp_path, fake)
        command = [older / "sandbox-ai"]
        code, out, err = run(command, env, "update", "--check")
        assert code == 0, out + err
        assert ("signature: none (the release has no SHA256SUMS.sig)" if case == "no signature" else
                "signature: BAD: not signed by a trusted release key") in out
        assert "an update refuses this release" in out
        found = status(command, env)
        assert found["result"]["newer"] is False and found["result"]["available"] == RELEASE
        code, out, err = run(command, env, "update", "mode")
        assert f"{RELEASE} is there, but " in out and ": not offered" in out
        calls = tmp_path / "stubs" / "calls"
        (tmp_path / "home" / ".local" / "state" / "sandbox-ai" / "update.json").unlink()
        run(command, env, "update", "--scheduled")
        assert not calls.exists() or "notify-send" not in calls.read_text(), "nothing announced"
    finally:
        fake.close()


def test_a_rate_limit_or_no_network_says_so(tmp_path, github, older):
    env = environment(tmp_path, github)
    command = [older / "sandbox-ai"]
    github.rate_limited = True
    code, out, err = run(command, env, "update", "--check")
    assert code == 1 and "api.github.com limits the requests to its API (HTTP 403)" in err, out + err
    assert "SANDBOX_AI_TOKEN" in err
    assert "failed: api.github.com limits the requests" in run(command, env, "update", "mode")[1]
    github.rate_limited = False
    offline = dict(env, https_proxy="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9")
    code, out, err = run(command, offline, "update", "--check")
    assert code == 1 and "cannot reach api.github.com" in err, out + err
    assert "failed: cannot reach api.github.com" in run(command, env, "update", "mode")[1]
