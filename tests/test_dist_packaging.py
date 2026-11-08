"""The ready-made packages' build: every GitHub action pinned by commit SHA, every download by SHA-256, each pinned
release at least 14 days old with its date beside it; the build scripts parse; make_dist.py collects the
packages into SHA256SUMS; a packaged sandbox-ai points `update` to the download."""
import datetime
import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
PINS = ROOT / "packaging" / "pins.sh"
REQUIREMENTS = ROOT / "packaging" / "requirements-app.txt"
BUILD_SCRIPTS = [ROOT / "scripts" / name for name in ("build_appimage.sh", "build_macos_app.sh", "e2e_package.sh")]
SH_SCRIPTS = [ROOT / "packaging" / name for name in ("AppRun", "macos-cli", "macos-app")]
MIN_AGE = datetime.timedelta(days=14)


def pins():
    values = {}
    for line in PINS.read_text().splitlines():
        match = re.fullmatch(r"([A-Z0-9_]+)=(\S*)", line.strip())
        if match:
            values[match[1]] = match[2]
    return values


def pinned_on():
    return datetime.date.fromisoformat(pins()["PINNED_ON"])


def test_every_action_is_pinned_by_a_commit_of_at_least_14_days():
    uses = []
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = re.match(r"\s*(?:-\s+)?uses:\s*(\S+)(.*)$", line)
            if match:
                uses.append((f"{workflow.name}:{number}", match[1], match[2]))
    assert uses
    for where, action, comment in uses:
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", action), f"{where}: {action} is not pinned by a commit SHA"
        dated = re.fullmatch(r"\s+#\s+v[\d.]+, published (\d{4}-\d{2}-\d{2})", comment)
        assert dated, f"{where}: {action} lacks '# vX.Y.Z, published YYYY-MM-DD'"
        assert datetime.date.fromisoformat(dated[1]) <= pinned_on() - MIN_AGE, f"{where}: younger than 14 days"


def test_one_release_of_an_action_is_one_commit():
    seen = {}
    for workflow in WORKFLOWS:
        for action, version in re.findall(r"uses:\s*([\w./-]+@[0-9a-f]{40})\s+#\s+(v[\d.]+)", workflow.read_text()):
            name, sha = action.split("@")
            assert seen.setdefault((name, version), sha) == sha, f"{name} {version} pinned to two commits"


def test_every_download_is_pinned_by_sha256_and_dated():
    values = pins()
    downloads = [key for key in values if key.endswith("_SHA256")]
    assert len(downloads) == 8  # 4 Pythons, 2 appimagetools, 2 runtimes
    for key in downloads:
        assert re.fullmatch(r"[0-9a-f]{64}", values[key]), key
        name = values[key[:-len("_SHA256")]]
        assert name and "continuous" not in name and "latest" not in name, key
    for group in ("PBS", "APPIMAGETOOL", "RUNTIME"):
        release, base = values[f"{group}_RELEASE"], values[f"{group}_BASE"]
        assert release not in ("", "continuous", "latest") and base.startswith("https://github.com/")
        assert base.endswith(f"/releases/download/{release}"), group
        assert datetime.date.fromisoformat(values[f"{group}_PUBLISHED"]) <= pinned_on() - MIN_AGE, group
    assert pinned_on() <= datetime.date.today()


def test_the_python_builds_are_the_same_release_and_python():
    values = pins()
    for key in ("PBS_LINUX_X86_64", "PBS_LINUX_AARCH64", "PBS_MACOS_ARM64", "PBS_MACOS_X86_64"):
        assert re.fullmatch(rf"cpython-{re.escape(values['PBS_PYTHON'])}\.\d+\+{values['PBS_RELEASE']}-"
                            r"[\w-]+-install_only_stripped\.tar\.gz", values[key]), key


def test_pyside6_is_pinned_by_hash_and_old_enough():
    text = REQUIREMENTS.read_text()
    requirements = [r for r in re.split(r"(?<!\\)\n", text) if r.strip() and not r.lstrip().startswith("#")]
    assert {r.split("==")[0] for r in requirements} == {"pyside6-essentials", "shiboken6"}
    for requirement in requirements:
        assert re.match(r"[\w-]+==[\d.]+ ", requirement), requirement
        hashes = re.findall(r"--hash=sha256:([0-9a-f]+)", requirement)
        assert len(hashes) == 3 and all(len(h) == 64 for h in hashes), requirement  # Linux x2, macOS
    uploaded = re.search(r"uploaded to PyPI on (\d{4}-\d{2}-\d{2})", text)
    assert uploaded and datetime.date.fromisoformat(uploaded[1]) <= pinned_on() - MIN_AGE
    assert "6.12.0" in text and "Python 3.13" in text and pins()["PBS_PYTHON"] == "3.13"  # 6.12.0 needs 3.12+


def test_the_build_scripts_download_only_through_the_checked_fetch():
    for script in BUILD_SCRIPTS[:2]:
        text = script.read_text()
        fetch = re.search(r"^fetch\(\) \{\n(.*?)^\}", text, re.M | re.S)[1]
        assert "curl " in fetch and "--proto '=https'" in fetch and "refused" in fetch
        # The one fallback (GitHub's API, when its /releases/download/ path fails) runs inside fetch() only and
        # writes the same .part file, so fetch's SHA-256 pin still decides; no other curl anywhere.
        fallback = re.search(r"^github_asset\(\) \{\n(.*?)^\}", text, re.M | re.S)[1]
        assert text.count("curl ") == fetch.count("curl ") + fallback.count("curl "), f"{script.name}: curl elsewhere"
        assert all("--proto '=https'" in line for line in fallback.splitlines() if "curl " in line)
        assert text.count("github_asset ") == 1 and 'github_asset "$url" "$file.part"' in fetch
        for call in re.findall(r"\$\(fetch ([^)]*)\)", text):
            assert re.fullmatch(r'"\$[A-Z_]+_BASE/[^"]+" "\$\w+" "\$\w+_sha"', call), call
        assert "--require-hashes" in text and "requirements-app.txt" in text
    assert "--runtime-file" in BUILD_SCRIPTS[0].read_text()  # never appimagetool's own download of "continuous"


@pytest.mark.parametrize("script", BUILD_SCRIPTS, ids=lambda p: p.name)
def test_the_build_scripts_parse(script):
    assert os.access(script, os.X_OK)
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SH_SCRIPTS, ids=lambda p: p.name)
def test_the_launchers_are_posix_sh(script):
    assert os.access(script, os.X_OK) and script.read_text().startswith("#!/bin/sh\n")
    subprocess.run(["sh", "-n", str(script)], check=True)
    if shutil.which("dash"):
        subprocess.run(["dash", "-n", str(script)], check=True)


def test_the_macos_cli_launcher_runs_sandbox_ai_from_the_bundle(tmp_path):
    """Contents/MacOS/sandbox-ai, reached through a link (as from /usr/local/bin), runs Resources' main.py."""
    contents = tmp_path / "sandbox-ai.app" / "Contents"
    (contents / "MacOS").mkdir(parents=True)
    (contents / "Resources" / "python" / "bin").mkdir(parents=True)
    (contents / "Resources" / "python" / "bin" / "python3").symlink_to(sys.executable)
    (contents / "Resources" / "sandbox-ai" / "sandbox").mkdir(parents=True)
    (contents / "Resources" / "sandbox-ai" / "sandbox" / "main.py").write_text(
        "import os, sys; print(sys.flags.isolated, sys.argv[1:], os.environ['PATH'].split(':')[-1])\n")
    shutil.copy(ROOT / "packaging" / "macos-cli", contents / "MacOS" / "sandbox-ai")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "sandbox-ai").symlink_to(contents / "MacOS" / "sandbox-ai")
    out = subprocess.run([str(tmp_path / "bin" / "sandbox-ai"), "list", "--json"], capture_output=True, text=True,
                         check=True).stdout
    assert out.strip() == "1 ['list', '--json'] /opt/podman/bin"


def test_the_release_collects_every_package_into_sha256sums():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    for job in ("build:", "e2e-pyz:", "build-appimage:", "e2e-appimage:", "build-macos:", "e2e-macos:", "release:"):
        assert f"\n  {job}" in text, job
    assert "ubuntu-24.04-arm" in text and "macos-15" in text and "macos-15-intel" in text
    assert '"3.9", "3.13"' in text and "debian:12" in text and "ubuntu:22.04" in text
    assert "make_dist.py --sums-only --out dist --add packages/*" in text
    assert "needs: [build, e2e-pyz, e2e-appimage, e2e-macos]" in text
    assert "--extract-and-run" in text and "e2e_package.sh macos-app" in text


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_packaging_tests", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_make_dist_adds_packages_and_rewrites_the_checksums(tmp_path):
    make_dist = load("make_dist")
    out = tmp_path / "dist"
    out.mkdir()
    (out / "sandbox-ai.pyz").write_bytes(b"pyz")
    (out / "SHA256SUMS").write_text("old\n")
    (out / "SHA256SUMS.sig").write_text("signed the old one\n")
    package = tmp_path / "packages" / "sandbox-ai-x86_64.AppImage"
    package.parent.mkdir()
    package.write_bytes(b"appimage")
    package.chmod(0o755)
    assert make_dist.main(["--sums-only", "--out", str(out), "--add", str(package)]) == 0
    sums = dict(reversed(line.split("  ")) for line in (out / "SHA256SUMS").read_text().splitlines())
    assert sums == {"sandbox-ai.pyz": hashlib.sha256(b"pyz").hexdigest(),
                    "sandbox-ai-x86_64.AppImage": hashlib.sha256(b"appimage").hexdigest()}
    assert not (out / "SHA256SUMS.sig").exists()  # it signed what is gone
    assert os.access(out / "sandbox-ai-x86_64.AppImage", os.X_OK)
    with pytest.raises(SystemExit, match="not a release file"):
        make_dist.main(["--sums-only", "--out", str(out), "--add", str(out / "SHA256SUMS")])


@pytest.mark.parametrize("kind", ["pyz", "appimage", "macos-app"])
def test_a_packaged_sandbox_ai_is_updated_by_download(tmp_path, kind):
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "sandbox", tree / "sandbox", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy(ROOT / "install.sh", tree / "install.sh")  # the bundle has it, but it must not run
    (tree / "PACKAGING").write_text(f"{kind}\n")
    env = {**os.environ, "XDG_DATA_HOME": str(tmp_path / "data"), "HOME": str(tmp_path / "home")}
    update = subprocess.run([sys.executable, "-I", str(tree / "sandbox" / "main.py"), "update"], capture_output=True,
                            text=True, timeout=60, env=env)
    assert update.returncode == 1 and "download the new one" in update.stderr
    assert not (tmp_path / "data").exists() and not (tmp_path / "home").exists()
    verbose = subprocess.run([sys.executable, "-I", str(tree / "sandbox" / "main.py"), "version", "--verbose"],
                             capture_output=True, text=True, timeout=60, env=env).stdout
    assert f"package: {kind}" in verbose


def test_update_check_in_a_package_reports_the_newest_release(tmp_path):
    """The app's update notice runs `sandbox-ai update --check`: in a package it names the package's version and
    the newest release (install.sh --check with a private data folder: an install.sh installation is not read)."""
    tree = tmp_path / "tree"
    shutil.copytree(ROOT / "sandbox", tree / "sandbox", ignore=shutil.ignore_patterns("__pycache__"))
    (tree / "install.sh").write_text('#!/usr/bin/env bash\necho "installed: 0.0.1 ($XDG_DATA_HOME)"\n'
                                     'echo "available: 9.9.9 (https://example.org)"\necho "args: $*"\n')
    (tree / "PACKAGING").write_text("appimage\n")
    result = subprocess.run([sys.executable, "-I", str(tree / "sandbox" / "main.py"), "update", "--check",
                             "--source", "https://example.org/o/r"], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    version = re.search(r'__version__ = "([^"]+)"', (ROOT / "sandbox/agent_sandbox.py").read_text())[1]
    assert lines[0] == f"installed: {version} (the AppImage (sandbox-ai-ARCH.AppImage))"
    assert "available: 9.9.9 (https://example.org)" in lines and "0.0.1" not in result.stdout
    assert "args: --check --source https://example.org/o/r" in lines
    assert lines[-1].startswith("update: this sandbox-ai runs from the AppImage")


def test_make_dist_refuses_to_add_files_a_build_removes(tmp_path):
    make_dist = load("make_dist")
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / "x.AppImage").write_bytes(b"x")
    with pytest.raises(SystemExit, match="removed by the build"):
        make_dist.main(["--out", str(tmp_path / "dist"), "--add", str(tmp_path / "dist" / "x.AppImage"), "--allow-dirty"])
    assert (tmp_path / "dist" / "x.AppImage").exists()


def fake_appdir(tmp_path):
    """An AppDir with the real AppRun and a stand-in for its Python that reports that it ran."""
    appdir = tmp_path / "appimage_extracted_0123"
    python = appdir / "usr/lib/sandbox-ai/tree/python/bin/python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\necho ran \"$@\"\n")
    python.chmod(0o755)
    shutil.copy(ROOT / "packaging" / "AppRun", appdir / "AppRun")
    return appdir


@pytest.mark.parametrize("mode", [0o777, 0o775, 0o757])
def test_apprun_refuses_an_appdir_others_can_write(tmp_path, mode):
    """Review finding (HIGH, multi-user): --appimage-extract-and-run reuses ${TMPDIR:-/tmp}/appimage_extracted_<hash>;
    one another user created (or can write) must not run. AppRun checks its folder and every parent before Python
    runs from it."""
    appdir = fake_appdir(tmp_path)
    out = subprocess.run([str(appdir / "AppRun"), "cli", "x"], capture_output=True, text=True, env={**os.environ, "APPDIR": ""})
    assert out.returncode == 0 and out.stdout.startswith("ran"), out.stderr
    assert out.stdout.split()[1:] == ["-I", str(appdir / "usr/lib/sandbox-ai/boot.py"), "cli", "x"]  # arguments kept
    appdir.chmod(mode)
    out = subprocess.run([str(appdir / "AppRun"), "cli", "x"], capture_output=True, text=True, env={**os.environ, "APPDIR": ""})
    assert out.returncode != 0 and "ran" not in out.stdout and "writable by others" in out.stderr
    appdir.chmod(0o755)
    (appdir / "usr").chmod(mode)  # a folder below, on the way to Python
    out = subprocess.run([str(appdir / "AppRun"), "cli", "x"], capture_output=True, text=True, env={**os.environ, "APPDIR": ""})
    assert out.returncode != 0 and "ran" not in out.stdout


def test_a_sticky_root_folder_above_is_fine(tmp_path):
    """/tmp itself (1777, root's) lies above every extracted AppImage: allowed."""
    sticky = tmp_path / "tmp"
    sticky.mkdir()
    sticky.chmod(0o1777)
    if sticky.stat().st_uid != 0:
        pytest.skip("needs a root-owned folder")
    appdir = fake_appdir(sticky)
    out = subprocess.run([str(appdir / "AppRun"), "cli"], capture_output=True, text=True, env={**os.environ, "APPDIR": ""})
    assert out.returncode == 0 and out.stdout.startswith("ran")


def test_extract_and_run_uses_a_private_tmpdir():
    """The README, the e2e checks and the build never let the runtime extract into the shared /tmp."""
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "--appimage-extract-and-run" in text
        for line in text.splitlines():
            if "--appimage-extract-and-run" in line and line.startswith(("TMPDIR", "./", "~")):
                assert "TMPDIR=$(mktemp -d)" in line, line
        assert "TMPDIR=$(mktemp -d)" in text
    e2e = (ROOT / "scripts" / "e2e_package.sh").read_text()
    assert re.search(r'export APPIMAGE_EXTRACT_AND_RUN=1 TMPDIR="\$work/tmp"', e2e)
    assert 'TMPDIR="$work" APPIMAGE_EXTRACT_AND_RUN=1' in (ROOT / "scripts" / "build_appimage.sh").read_text()


def test_ci_installs_only_hash_pinned_packages():
    """Review finding: `pip install pytest build` and the isolated build's own setuptools were unpinned."""
    for workflow in WORKFLOWS:
        for line in workflow.read_text().splitlines():
            if "pip install" in line and "e2e" not in workflow.name and "dist/*.whl" not in line:  # our own wheel
                assert "--require-hashes" in line and "-r packaging/requirements-ci.txt" in line, line
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "python scripts/make_dist.py --no-isolation" in release
    assert '"--no-isolation"' in (ROOT / "scripts/make_dist.py").read_text()
    text = (ROOT / "packaging" / "requirements-ci.txt").read_text()
    requirements = [r for r in re.split(r"(?<!\\)\n", text) if r.strip() and not r.lstrip().startswith("#")]
    assert {r.split("==")[0] for r in requirements} >= {"pytest", "build", "setuptools"}
    for requirement in requirements:
        name, version = re.match(r"([\w-]+)==([\d.]+)", requirement).groups()
        assert re.search(r"--hash=sha256:[0-9a-f]{64}", requirement), requirement
        uploaded = re.search(rf"# {re.escape(name)} {re.escape(version)}: uploaded (\d{{4}}-\d{{2}}-\d{{2}})", text)
        assert uploaded and datetime.date.fromisoformat(uploaded[1]) <= pinned_on() - MIN_AGE, requirement


def test_a_release_bundles_the_app_of_its_own_commit():
    """The app is part of this repository: a release takes it from the same checkout, never from a moving branch of
    another repository."""
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "repository:" not in release and "SANDBOX_AI_APP_REF" not in release
    assert (ROOT / "projects/sandbox-ai-app/sandbox_ai_app/__main__.py").is_file()
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "working-directory: projects/sandbox-ai-app" in ci  # its tests run in CI


@pytest.mark.parametrize("script", BUILD_SCRIPTS[:2], ids=lambda p: p.name)
def test_the_package_builds_are_reproducible_and_name_no_build_folder(script):
    """Review finding: pip's console scripts (pyside/bin) and PySide6's file-time .pyc named the builder's mktemp
    folder, and file times made every build differ. The builds drop pyside/bin, compile PySide6 hash-based too,
    set every file time to SOURCE_DATE_EPOCH and fail when the work folder's path is still found inside."""
    text = script.read_text()
    assert re.search(r'rm -rf -- "\$\w+/pyside/bin"', text) or 'rm -rf -- "$resources/pyside/bin"' in text
    assert '"$lib/pyside"' in text or '"$resources/pyside"' in text
    assert "SOURCE_DATE_EPOCH" in text and ('touch -h -d "@$epoch"' in text or 'touch -h -t "$stamp"' in text)
    assert 'grep -rlF -- "$work"' in text and "names the build folder" in text
    assert "dist-info/RECORD" in text  # pip's RECORD lists the removed commands with their hashes
    assert "no build path inside" in (ROOT / "scripts" / "e2e_package.sh").read_text()


def test_workflow_hardening():
    """Review finding: checkouts kept the token in .git/config, run: blocks expanded ${{ github.* }} into shell
    code, a workflow_dispatch on a tag would have published, and a comment still named #!/usr/bin/env python3."""
    for workflow in WORKFLOWS:
        lines = workflow.read_text().splitlines()
        for number, line in enumerate(lines):
            if re.search(r"uses: actions/checkout@", line):
                block = []
                for following in lines[number + 1:]:
                    if re.match(r"\s*- ", following) or not following.strip().startswith(("with:", "repository:",
                                                                                          "ref:", "path:", "persist")):
                        break
                    block.append(following.strip())
                assert "persist-credentials: false" in block, f"{workflow.name}:{number + 1}"
        text = workflow.read_text()
        for match in re.finditer(r"\$\{\{\s*(github\.(?:server_url|repository|ref_name|head_ref|event)[^}]*)\}\}", text):
            line = text[text.rfind("\n", 0, match.start()) + 1:text.find("\n", match.start())]
            assert not line.lstrip().startswith(("run:", "python", "gh ", "echo")), f"{workflow.name}: {line}"
    release = (ROOT / ".github/workflows/release.yml").read_text()
    for job in ("release", "pypi"):
        head = release[release.index(f"\n  {job}:"):]
        head = head[:head.index("steps:")]
        assert "github.event_name == 'push'" in head and "startsWith(github.ref, 'refs/tags/')" in head, job
    assert "#!/usr/bin/env python3" not in release


def test_one_line_run_values_are_plain_yaml():
    """`run: pip install --only-binary :all: -r ...` is not YAML (": " inside a plain value): quote such lines."""
    for workflow in WORKFLOWS:
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = re.match(r"\s*(?:- )?(run|name|if): (.*)$", line)
            if match and match[2] and match[2][0] not in "|>\"'":
                assert ": " not in match[2] and " #" not in match[2].split("#")[0][:-1], f"{workflow.name}:{number}"
