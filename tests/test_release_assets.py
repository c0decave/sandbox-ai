"""The files of a release, in one place (sandbox/release_assets.py): the names the updaters ask for are the names
scripts/make_dist.py and .github/workflows/release.yml build, scripts/publish_release.py refuses a release that lacks
one, and scripts/release_check.py says what a release - a folder, or one on GitHub, published or a draft - holds."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import fake_github  # noqa: E402
import release_kit as kit  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
V = "1.2.3"
needs_tools = pytest.mark.skipif(not (fake_github.available() and shutil.which("ssh-keygen")),
                                 reason="needs the openssl and ssh-keygen commands")


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


assets = load(ROOT / "sandbox/release_assets.py", "release_assets_under_test")
EVERY = ["install.sh", "sandbox-ai-x86_64.AppImage", "sandbox-ai-aarch64.AppImage", "sandbox-ai-arm64.dmg",
         "sandbox-ai-x86_64.dmg", "sandbox-ai.pyz", f"sandbox_ai_app-{V}-py3-none-any.whl",
         f"sandbox_ai_app-{V}.tar.gz", f"sandbox-ai-bundle-{V}.tar.gz", "sandbox-ai-videos.tar"]


# -- the list ---------------------------------------------------------------------------------------------------------

def test_a_release_is_these_files():
    """The user's list (2026-11-13): both AppImages, both disk images, the .pyz, wheel, sdist, bundle, install.sh, the
    videos - all listed in SHA256SUMS, which is signed."""
    assert [entry["name"] for entry in assets.artifacts(V)] == EVERY
    assert assets.required(V) == [*EVERY, "SHA256SUMS", "SHA256SUMS.sig"]
    assert assets.required(V, signed=False)[-1] == "SHA256SUMS"
    # What release.yml builds: everything but the videos (rendered where the release is signed) and the signature.
    assert assets.required(V, by="ci", signed=False) == [name for name in EVERY if name != "sandbox-ai-videos.tar"] + \
        ["SHA256SUMS"]
    assert assets.missing(EVERY + ["SHA256SUMS", "SHA256SUMS.sig"], V) == []
    assert assets.missing(["install.sh", "SHA256SUMS"], V)[:2] == ["sandbox-ai-x86_64.AppImage",
                                                                   "sandbox-ai-aarch64.AppImage"]
    assert assets.unknown([*EVERY, "sandbox-ai-bundle-1.2.2.tar.gz", "notes.txt"], V) == \
        ["notes.txt", "sandbox-ai-bundle-1.2.2.tar.gz"]


@pytest.mark.parametrize("kind, machine, name", [
    ("user", "x86_64", f"sandbox-ai-bundle-{V}.tar.gz"), ("system", "aarch64", f"sandbox-ai-bundle-{V}.tar.gz"),
    ("pip", "arm64", f"sandbox_ai_app-{V}-py3-none-any.whl"), ("pyz", "riscv64", "sandbox-ai.pyz"),
    ("appimage", "x86_64", "sandbox-ai-x86_64.AppImage"), ("appimage", "aarch64", "sandbox-ai-aarch64.AppImage"),
    ("appimage", "arm64", "sandbox-ai-aarch64.AppImage"), ("appimage", "AMD64", "sandbox-ai-x86_64.AppImage"),
    ("macos-app", "arm64", "sandbox-ai-arm64.dmg"), ("macos-app", "x86_64", "sandbox-ai-x86_64.dmg"),
    ("appimage", "riscv64", None), ("macos-app", "ppc", None), ("checkout", "x86_64", None),
])
def test_every_kind_of_installation_downloads_a_file_the_release_has(kind, machine, name):
    assert assets.asset_for(kind, V, machine) == name
    if name is not None:
        assert name in assets.required(V)


def test_the_version_of_a_release_is_its_bundles():
    assert assets.bundle_version(EVERY) == V
    assert assets.bundle_version(["sandbox-ai.pyz", "SHA256SUMS"]) is None
    assert assets.bundle_version([f"sandbox-ai-bundle-{V}.tar.gz", "sandbox-ai-bundle-9.9.9.tar.gz"]) is None
    assert assets.bundle_version(["sandbox-ai-bundle-1.2.3-rc1.tar.gz"]) == "1.2.3-rc1"
    text = f"{'a' * 64}  sandbox-ai.pyz\n{'b' * 64} *install.sh\nnot a line\nzz  short\n"
    assert assets.checksums(text) == {"sandbox-ai.pyz": "a" * 64, "install.sh": "b" * 64}


def test_install_sh_and_the_updaters_read_the_same_bundle_name():
    """install.sh runs before any tree is installed, so it carries its own copy of the pattern."""
    installer = (ROOT / "install.sh").read_text()
    pattern = re.search(r'^ASSET = re\.compile\(r"(.+)"\)$', installer, re.M)[1]
    assert pattern == assets.BUNDLE.pattern
    updates = (ROOT / "sandbox/updates.py").read_text()
    assert 'VERSION = re.compile(r"' + assets.VERSION + '")' in updates


def test_the_scripts_take_the_names_from_the_one_list():
    make_dist = load(ROOT / "scripts/make_dist.py", "make_dist_for_assets")
    assert make_dist.VIDEOS == assets.VIDEOS and tuple(make_dist.python_package_files(V)) == \
        (assets.wheel(V), assets.sdist(V))
    notes = load(ROOT / "scripts/release_notes.py", "release_notes_for_assets").notes(V)
    for name in assets.required(V):
        assert f"`{name}`" in notes, f"the release page does not name {name}"
    releasing = (ROOT / "RELEASING.md").read_text()
    for name in assets.required("X.Y.Z"):
        assert f"`{name}`" in releasing, f"RELEASING.md's checklist does not name {name}"


def test_release_yml_builds_every_file_ci_can_build_and_checks_the_set():
    workflow = (ROOT / ".github/workflows/release.yml").read_text()
    appimages = re.search(r"build-appimage:.*?matrix:\s+include:(.*?)runs-on:", workflow, re.S)[1]
    assert sorted(re.findall(r"arch: (\S+)", appimages)) == sorted(assets.APPIMAGE_ARCHES)
    macos = re.search(r"build-macos:.*?matrix:\s+include:(.*?)runs-on:", workflow, re.S)[1]
    assert sorted(re.findall(r"arch: (\S+)", macos)) == sorted(assets.MACOS_ARCHES)
    assert "path: packages/sandbox-ai-${{ matrix.arch }}.AppImage" in workflow
    assert "path: packages/sandbox-ai-${{ matrix.arch }}.dmg" in workflow
    assert workflow.count("if-no-files-found: error") >= 3, "an upload of nothing must fail the job"
    release = workflow[workflow.index("\n  release:"):workflow.index("\n  pypi:")]
    assert "needs: [build, e2e-pyz, e2e-appimage, e2e-macos]" in release
    # After SHA256SUMS is written over dist/ and the packages: every file CI builds is there, listed and works.
    sums, check = release.index("make_dist.py --sums-only --out dist --add packages/*"), \
        release.index("scripts/release_check.py --dir dist --ci")
    assert sums < check < release.index("GitHub release"), "the set is checked before anything is published"
    # The update tests against a fake GitHub run with the suite of a release.
    assert "python -m pytest -q -rfE tests" in workflow


# -- publish_release.py refuses an incomplete release -----------------------------------------------------------------

def complete(folder, version=V, without=(), sign=None):
    """A folder with every file of release VERSION (a few bytes each) and SHA256SUMS over them."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in assets.required(version, signed=False)[:-1]:
        if name not in without:
            (folder / name).write_bytes(f"{name} of {version}\n".encode())
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in sorted(folder.iterdir())]
    (folder / "SHA256SUMS").write_text("".join(lines))
    if sign is not None:
        signature = subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(sign.path), "-n", "sandbox-ai-release"],
                                   input=(folder / "SHA256SUMS").read_bytes(), capture_output=True, check=True).stdout
        (folder / "SHA256SUMS.sig").write_bytes(signature)
    return sorted(folder.iterdir())


@pytest.fixture
def publish(monkeypatch):
    """publish_release.py with no release key listed (the checks of the list itself)."""
    module = load(ROOT / "scripts/publish_release.py", "publish_release_for_assets")
    keys = module.release_keys()
    monkeypatch.setattr(module, "release_keys", lambda: keys)
    monkeypatch.setattr(keys, "load", lambda *args, **kwargs: [])
    return module


@pytest.mark.parametrize("gone", EVERY)
def test_publish_release_refuses_a_release_that_lacks_a_file(gone, tmp_path, publish):
    files = complete(tmp_path / "dist", without=(gone,))
    with pytest.raises(SystemExit, match=f"release {re.escape(V)} lacks {re.escape(gone)}"):
        publish.check_files(files, None, False, version=V)
    # A release on one's own Gitea without the packages CI builds: said, and allowed with --partial only.
    assert publish.check_files(files, None, False, version=V, partial=True) == files
    with pytest.raises(SystemExit, match="--partial is not for GitHub"):
        publish.check_files(files, None, False, version=V, partial=True, kind="github")


def test_publish_release_takes_the_complete_set_and_refuses_another_versions_files(tmp_path, publish):
    files = complete(tmp_path / "dist")
    assert publish.check_files(files, None, False, version=V, kind="github") == files
    with pytest.raises(SystemExit, match="sandbox-ai-bundle-1.2.3.tar.gz is not a file of release 1.2.4"):
        publish.check_files(files, None, False, version="1.2.4")  # (the tag of another version)
    stale = tmp_path / "dist" / "sandbox-ai-bundle-1.2.2.tar.gz"
    stale.write_bytes(b"old")
    complete(tmp_path / "dist")
    with pytest.raises(SystemExit, match="sandbox-ai-bundle-1.2.2.tar.gz is not a file of release 1.2.3"):
        publish.check_files(sorted((tmp_path / "dist").iterdir()), None, False, version=V)


# -- against GitHub: a draft first, published only when every file is up ----------------------------------------------

@pytest.fixture(scope="module")
def signed(tmp_path_factory):
    """A copy of this checkout that trusts a test key (its scripts check with that key), and the key."""
    base = tmp_path_factory.mktemp("assets")
    key = kit.new_key(base / "keys")
    tree = base / "tree"
    for name in ("scripts", "sandbox/release_keys.py", "sandbox/release_assets.py", "sandbox/package_update.py",
                 "sandbox/release-signers", "sandbox/agent_sandbox.py", "docs/releases"):
        source = ROOT / name
        (tree / name).parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, tree / name, ignore=shutil.ignore_patterns("__pycache__"))
        else:
            shutil.copy2(source, tree / name)
    kit.set_keys(tree, [key])
    kit.set_version(tree, V)
    yield SimpleNamespace(base=base, key=key, tree=tree, evil=kit.new_key(base / "keys", "evil"))
    kit.remove(base)


@pytest.fixture
def github(signed):
    fake = fake_github.FakeGitHub(signed.base / "tls")
    fake.pushed_tags.add(f"v{V}")
    yield fake
    fake.close()
    assert fake.leaked == [] and fake.refused == []


def script(signed, github, name, *args, **extra):
    env = github.env({key: value for key, value in os.environ.items() if not key.startswith("SANDBOX_AI_")})
    env.update(SANDBOX_AI_TOKEN="0123456789abcdef0123456789abcdef01234567", TMPDIR=str(signed.base), **extra)
    return subprocess.run([sys.executable, str(signed.tree / "scripts" / name), *map(str, args)], capture_output=True,
                          text=True, env=env, timeout=300)


@needs_tools
def test_publish_release_to_github_is_a_draft_until_every_file_is_up(tmp_path, signed, github):
    files = complete(tmp_path / "dist", sign=signed.key)
    done = script(signed, github, "publish_release.py", "--source", github.source, *files)
    assert done.returncode == 0, done.stderr
    release = github.releases[f"v{V}"]
    assert sorted(release["files"]) == sorted(assets.required(V))
    assert release["draft"] is False and github.newest() is release
    posts = [(host, path) for method, host, path, _ in github.requests if method == "POST"]
    assert posts[0] == ("api.github.com", "/repos/c0decave/sandbox-ai/releases") and \
        all(host == "uploads.github.com" for host, _ in posts[1:])
    assert github.patches == [(f"v{V}", {"draft": False})], "published once, after the last upload"
    assert f"publish_release: published release v{V}" in done.stderr
    # Every request that carried the token went to GitHub's API or its upload host.
    assert {host for _, host, _, token in github.requests if token} <= {"api.github.com", "uploads.github.com"}


@needs_tools
def test_an_incomplete_set_never_reaches_github(tmp_path, signed, github):
    files = complete(tmp_path / "dist", without=("sandbox-ai-arm64.dmg",), sign=signed.key)
    done = script(signed, github, "publish_release.py", "--source", github.source, *files)
    assert done.returncode != 0 and "release 1.2.3 lacks sandbox-ai-arm64.dmg" in done.stderr
    partial = script(signed, github, "publish_release.py", "--source", github.source, "--partial", *files)
    assert partial.returncode != 0 and "--partial is not for GitHub" in partial.stderr
    assert github.releases == {} and github.uploads == []


@needs_tools
def test_publish_release_draft_leaves_the_release_for_the_check_and_publish_ends_it(tmp_path, signed, github):
    files = complete(tmp_path / "dist", sign=signed.key)
    draft = script(signed, github, "publish_release.py", "--source", github.source, "--draft", *files)
    assert draft.returncode == 0, draft.stderr
    release = github.releases[f"v{V}"]
    assert release["draft"] is True and github.newest() is None, "nobody is served a draft"
    assert "scripts/release_check.py 1.2.3" in draft.stderr and "--publish" in draft.stderr
    # A second run finds the draft (GitHub does not find a draft by its tag) instead of making another one.
    again = script(signed, github, "publish_release.py", "--source", github.source, "--draft", *files)
    assert again.returncode == 0 and len(github.releases) == 1, again.stderr
    assert sum(1 for method, _, path, _ in github.requests if method == "POST" and path.endswith("/releases")) == 1
    published = script(signed, github, "publish_release.py", "--source", github.source, "--publish")
    assert published.returncode == 0, published.stderr
    assert release["draft"] is False and github.newest() is release


@needs_tools
def test_publish_refuses_to_end_a_draft_that_lacks_a_file(tmp_path, signed, github):
    files = complete(tmp_path / "dist", sign=signed.key)
    assert script(signed, github, "publish_release.py", "--source", github.source, "--draft", *files).returncode == 0
    release = github.releases[f"v{V}"]
    number = release["ids"].pop("sandbox-ai.pyz")
    release["files"].pop("sandbox-ai.pyz")
    github.assets.pop(number)
    refused = script(signed, github, "publish_release.py", "--source", github.source, "--publish")
    assert refused.returncode != 0 and "lacks sandbox-ai.pyz" in refused.stderr
    assert release["draft"] is True


# -- release_check.py -------------------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real(tmp_path_factory):
    """A real release of this checkout as version 1.2.3 (make_dist.py; stand-ins for CI's packages), signed."""
    base = tmp_path_factory.mktemp("real")
    key = kit.new_key(base / "keys")
    tree = kit.tree_copy(base / "tree", V, [key])
    dist = kit.full_release(tree, base / "dist", key, base / "work")
    yield SimpleNamespace(base=base, key=key, tree=tree, dist=dist, evil=kit.new_key(base / "keys", "evil"))
    kit.remove(base)


def check(real, *args, env=None):
    env = {**(env or os.environ), "TMPDIR": str(real.base)}
    return subprocess.run([sys.executable, str(real.tree / "scripts/release_check.py"), *map(str, args)],
                          capture_output=True, text=True, env=env, timeout=900)


@needs_tools
def test_release_check_of_a_folder_says_every_file_is_there_listed_signed_and_works(real):
    done = check(real, V, "--dir", real.dist, "--json")
    assert done.returncode == 0, done.stdout + done.stderr
    report = json.loads(done.stdout)
    assert report["version"] == V and report["ready"] is True and report["problems"] == []
    assert report["signature"] == {"state": "good", "signer": real.key.fingerprint}
    rows = {row["name"]: row for row in report["files"]}
    assert list(rows) == assets.required(V)
    for name, row in rows.items():
        assert row["present"] is True, name
        if name not in ("SHA256SUMS", "SHA256SUMS.sig"):
            assert row["listed"] is True and row["sha256"] == "ok", name
    assert rows["sandbox-ai.pyz"]["test"] == f"ok: --version says sandbox-ai {V}"
    assert rows[assets.wheel(V)]["test"] == f"ok: sandbox-ai-app {V}, the commands sandbox-ai and sandbox-ai-app"
    assert rows[assets.bundle(V)]["test"].startswith(f"ok: sandbox-ai {V}, trusts the signing key")
    assert rows["install.sh"]["test"].startswith("ok: bash -n, carries the signing key")
    machine = assets.arch_of("appimage")
    if machine:
        assert rows[assets.appimage(machine)]["test"] == f"ok: its manifest says appimage {V}"
    assert rows["sandbox-ai-videos.tar"]["test"] == "ok: 1 video, 1 subtitle file"
    table = check(real, V, "--dir", real.dist)
    assert table.returncode == 0 and f"sandbox-ai {V}: READY" in table.stdout, table.stdout
    assert re.search(r"sandbox-ai\.pyz\s+yes\s+yes\s+ok\s+ok: --version says", table.stdout)


@needs_tools
@pytest.mark.parametrize("case, words", [
    ("missing", "sandbox-ai-arm64.dmg is missing"),
    ("unlisted", "sandbox-ai.pyz is not listed in SHA256SUMS"),
    ("changed", "sandbox-ai-videos.tar does not match SHA256SUMS"),
    ("unsigned", "SHA256SUMS.sig is missing"),
    ("another-key", "which is not a trusted release key"),
    ("another-version", f"says it is sandbox-ai 1.2.4, not {V}"),
    ("stray", "sandbox-ai-bundle-1.2.2.tar.gz is not a file of release"),
])
def test_release_check_names_what_is_wrong(case, words, real, tmp_path):
    folder = tmp_path / "dist"
    folder.mkdir()
    for path in real.dist.iterdir():
        os.link(path, folder / path.name)
    sums = folder / "SHA256SUMS"

    def resign(text=None, key=real.key):
        if text is not None:
            sums.unlink()
            sums.write_text(text)
        (folder / "SHA256SUMS.sig").unlink(missing_ok=True)
        (folder / "SHA256SUMS.sig").write_bytes(subprocess.run(
            ["ssh-keygen", "-Y", "sign", "-f", str(key.path), "-n", "sandbox-ai-release"], input=sums.read_bytes(),
            capture_output=True, check=True).stdout)

    if case == "missing":
        (folder / "sandbox-ai-arm64.dmg").unlink()
    elif case == "unlisted":
        resign("".join(line for line in sums.read_text().splitlines(True) if "sandbox-ai.pyz" not in line))
    elif case == "changed":
        (folder / "sandbox-ai-videos.tar").unlink()
        (folder / "sandbox-ai-videos.tar").write_bytes((real.dist / "sandbox-ai-videos.tar").read_bytes() + b"x")
    elif case == "unsigned":
        (folder / "SHA256SUMS.sig").unlink()
    elif case == "another-key":
        resign(key=real.evil)
    elif case == "another-version":
        other = kit.tree_copy(tmp_path / "other", "1.2.4", [real.key])
        built = kit.make_dist(other, tmp_path / "other-dist", "--skip-python")
        (folder / "sandbox-ai.pyz").unlink()
        shutil.copy2(built / "sandbox-ai.pyz", folder / "sandbox-ai.pyz")
        text = "".join(line for line in sums.read_text().splitlines(True) if "sandbox-ai.pyz" not in line)
        resign(text + f"{kit.sha256(folder / 'sandbox-ai.pyz')}  sandbox-ai.pyz\n")
    elif case == "stray":
        (folder / "sandbox-ai-bundle-1.2.2.tar.gz").write_bytes(b"old")
    # (--no-try where the try is not what is wrong: the .pyz's start alone takes some 20 s)
    done = check(real, V, "--dir", folder, *([] if case == "another-version" else ["--no-try"]))
    assert done.returncode == 1, done.stdout
    assert words in done.stdout, done.stdout
    assert f"sandbox-ai {V}: NOT READY" in done.stdout


@needs_tools
def test_release_check_takes_folders_relative_to_where_it_runs(real, tmp_path):
    """--dir and --work as relative paths (audit 2026-11-20): the AppImage's try started the copy in --work by its
    relative path from inside that folder - "could not be tried: No such file or directory", NOT READY."""
    env = {**os.environ, "TMPDIR": str(real.base)}
    done = subprocess.run([sys.executable, str(real.tree / "scripts/release_check.py"), V, "--dir",
                           os.path.relpath(real.dist, tmp_path), "--work", "work"], cwd=tmp_path, env=env,
                          capture_output=True, text=True, timeout=900)
    assert done.returncode == 0 and f"sandbox-ai {V}: READY" in done.stdout, done.stdout + done.stderr
    machine = assets.arch_of("appimage")
    if machine:
        assert re.search(rf"{re.escape(assets.appimage(machine))}\s+yes\s+yes\s+ok\s+ok: its manifest", done.stdout)


@needs_tools
def test_release_check_of_what_ci_built_needs_neither_videos_nor_signature(real, tmp_path):
    folder = tmp_path / "dist"
    folder.mkdir()
    for path in real.dist.iterdir():
        if path.name not in ("SHA256SUMS.sig", "sandbox-ai-videos.tar", "SHA256SUMS"):
            os.link(path, folder / path.name)
    kit.sums_only(real.tree, folder)
    done = check(real, "--dir", folder, "--ci")
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"sandbox-ai {V}: every file CI builds is there" in done.stdout
    (folder / "sandbox-ai-aarch64.AppImage").unlink()
    done = check(real, "--dir", folder, "--ci", "--no-try")
    assert done.returncode == 1 and "sandbox-ai-aarch64.AppImage is missing" in done.stdout


@needs_tools
def test_release_check_of_a_release_on_github_published_or_a_draft(real):
    fake = fake_github.FakeGitHub(real.base / "tls")
    try:
        env = fake.env({key: value for key, value in os.environ.items() if not key.startswith("SANDBOX_AI_")})
        fake.publish(V, real.dist, draft=True)
        hidden = check(real, V, "--source", fake.source, env=env)
        assert hidden.returncode == 1 and "no release v1.2.3" in hidden.stdout + hidden.stderr
        assert "a draft is only shown to a token" in hidden.stdout + hidden.stderr
        token = {**env, "SANDBOX_AI_TOKEN": "0123456789abcdef0123456789abcdef01234567"}
        draft = check(real, V, "--source", fake.source, env=token)
        assert draft.returncode == 0, draft.stdout + draft.stderr
        assert f"sandbox-ai {V}: READY" in draft.stdout and "a draft: not published yet" in draft.stdout
        fake.releases[f"v{V}"]["draft"] = False
        published = check(real, V, "--source", fake.source, "--json", env=env)
        assert published.returncode == 0, published.stdout + published.stderr
        report = json.loads(published.stdout)
        assert report["ready"] is True and report["release"] == {"tag": f"v{V}", "draft": False, "latest": True,
                                                                  "source": fake.source}
        assert fake.leaked == [] and fake.refused == []
    finally:
        fake.close()
