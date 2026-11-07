"""sandbox-ai.pyz (scripts/make_pyz.py, scripts/dist_boot.py): reproducible, every file checked against the
manifest, extracted once into a private cache folder, refused once that copy was changed, main.py executed
isolated with the arguments; the AppImage's start (the same boot from a folder) picks the app or the command line."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import threading
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = next(line.split('"')[1] for line in (ROOT / "sandbox/agent_sandbox.py").read_text().splitlines()
               if line.startswith("__version__ = "))


def load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_for_tests", ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


make_pyz = load("make_pyz")
boot = load("dist_boot")

# A stand-in for sandbox-ai: main.py reports how it was started.
FAKE_MAIN = b"""import json, sys
print(json.dumps({"argv": sys.argv, "isolated": sys.flags.isolated, "executable": sys.executable,
                  "file": __file__}))
"""


def fake_bundle(path, extra=None, mtime=1700000000):
    files = {"sandbox/agent_sandbox.py": (b'__version__ = "9.8.7"\n', 0o644),
             "sandbox/main.py": (FAKE_MAIN, 0o644),
             "sandbox/helper.py": (b"VALUE = 1\n", 0o644),
             "sandbox-ai": (b"#!/bin/sh\n", 0o755),
             "tests/test_x.py": (b"def test(): pass\n", 0o644),
             **(extra or {})}
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for name in ["", "sandbox", "tests"]:
            info = tarfile.TarInfo(f"sandbox-ai-9.8.7/{name}".rstrip("/"))
            info.type, info.mode, info.mtime = tarfile.DIRTYPE, 0o755, mtime
            tar.addfile(info)
        for name, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(f"sandbox-ai-9.8.7/{name}")
            info.size, info.mode, info.mtime = len(data), mode, mtime
            tar.addfile(info, io.BytesIO(data))
    import gzip
    with open(path, "wb") as stream, gzip.GzipFile(fileobj=stream, mode="wb", mtime=mtime, filename="") as gz:
        gz.write(raw.getvalue())
    return path


@pytest.fixture
def pyz(tmp_path):
    bundle = fake_bundle(tmp_path / "bundle.tar.gz")
    return make_pyz.build_pyz(bundle, tmp_path / "sandbox-ai.pyz")


def env_for(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PYTHON", "XDG_"))}
    env.update({"XDG_CACHE_HOME": str(tmp_path / "cache"), "HOME": str(tmp_path / "home")}, **extra)
    return env


def run(pyz, tmp_path, *args, **extra):
    return subprocess.run([sys.executable, str(pyz), *args], capture_output=True, text=True, timeout=120,
                          env=env_for(tmp_path, **extra))


def copies(tmp_path):
    root = tmp_path / "cache" / "sandbox-ai" / "pyz"
    return sorted(p for p in root.iterdir() if not p.name.startswith(".")) if root.is_dir() else []


def test_the_pyz_is_reproducible(tmp_path, monkeypatch):
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    first = make_pyz.build_pyz(fake_bundle(tmp_path / "a.tar.gz"), tmp_path / "a.pyz")
    second = make_pyz.build_pyz(fake_bundle(tmp_path / "b.tar.gz"), tmp_path / "b.pyz")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().startswith(b"#!/bin/sh\n") and b'exec python3 -I "$0" "$@"\n' in first.read_bytes()[:200]
    with zipfile.ZipFile(first) as archive:
        names = archive.namelist()
        assert names[:2] == ["__main__.py", "manifest.json"] and names[2:] == sorted(names[2:])
        assert {info.date_time for info in archive.infolist()} == {(2023, 11, 14, 22, 13, 20)}  # the bundle's time
        assert archive.read("__main__.py") == (ROOT / "scripts/dist_boot.py").read_bytes()
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("tree/PACKAGING") == b"pyz\n"
    assert manifest["version"] == "9.8.7" and manifest["kind"] == "pyz" and manifest["compile"] == ["sandbox"]
    files = {name: (sha, size, mode) for name, sha, size, mode in manifest["files"]}
    assert files["sandbox/main.py"] == (hashlib.sha256(FAKE_MAIN).hexdigest(), len(FAKE_MAIN), 0o644)
    assert files["sandbox-ai"][2] == 0o755
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1000000000")
    third = make_pyz.build_pyz(fake_bundle(tmp_path / "c.tar.gz"), tmp_path / "c.pyz")
    assert third.read_bytes() != first.read_bytes()
    with zipfile.ZipFile(third) as archive:
        assert {info.date_time for info in archive.infolist()} == {(2001, 9, 9, 1, 46, 40)}


def test_the_real_bundle_runs_as_a_pyz(tmp_path):
    """make_dist.py's bundle of this checkout, as sandbox-ai.pyz: --version, and version --verbose names it."""
    make_dist = load("make_dist")
    bundle = make_dist.bundle(tmp_path, VERSION)
    pyz = make_pyz.build_pyz(bundle, tmp_path / "sandbox-ai.pyz", epoch=0)
    result = run(pyz, tmp_path, "--version")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"sandbox-ai {VERSION}"
    verbose = run(pyz, tmp_path, "version", "--verbose")
    assert "package: pyz" in verbose.stdout and "sandbox-ai.pyz: download the new one" in verbose.stdout
    update = run(pyz, tmp_path, "update")
    assert update.returncode == 1 and "download the new one" in update.stderr  # install.sh is never run
    [copy] = copies(tmp_path)
    assert (copy / "sandbox" / "__pycache__").is_dir()  # precompiled once
    assert not (copy / "tests" / "__pycache__").exists()


def test_main_py_is_executed_isolated_with_the_arguments(pyz, tmp_path):
    result = run(pyz, tmp_path, "claude", "--dry-run", "a b", "--")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    [copy] = copies(tmp_path)
    assert report["argv"][1:] == ["claude", "--dry-run", "a b", "--"]
    assert report["isolated"] == 1
    assert report["file"] == str(copy / "sandbox" / "main.py")
    assert os.path.realpath(report["executable"]) == os.path.realpath(sys.executable)
    assert copy.name.startswith("9.8.7-") and copy.name.endswith(sys.implementation.cache_tag)


def test_the_shebang_starts_it(pyz, tmp_path):
    result = subprocess.run([str(pyz), "x"], capture_output=True, text=True, timeout=60,
                            env={**env_for(tmp_path), "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}"})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["argv"][1:] == ["x"]


def test_the_copy_is_made_once_private_and_read_only(pyz, tmp_path):
    for _ in range(3):
        assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    root = copy.parent
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert not [p for p in root.iterdir() if p.name.startswith(".")]  # no temporary folder left
    for path in [copy / "sandbox" / "main.py", copy / "sandbox" / "helper.py", copy / "PACKAGING"]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o444
    assert stat.S_IMODE((copy / "sandbox-ai").stat().st_mode) == 0o555
    assert stat.S_IMODE((copy / "sandbox").stat().st_mode) == 0o500
    pycs = list((copy / "sandbox" / "__pycache__").glob("helper.*.pyc"))
    assert pycs and stat.S_IMODE(pycs[0].stat().st_mode) == 0o444
    marker = json.loads((copy / boot.MARKER).read_text())
    assert "sandbox/main.py" in marker["entries"] and "sandbox/__pycache__" in marker["entries"]


def writable(path):
    os.chmod(path.parent, 0o700)
    if path.exists():
        os.chmod(path, 0o644)


@pytest.mark.parametrize("change", ["content", "mode", "added", "removed", "replaced", "marker", "pyc", "top"])
def test_a_changed_copy_is_refused(pyz, tmp_path, change):
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    main = copy / "sandbox" / "main.py"
    if change == "content":
        writable(main)
        main.write_bytes(b"print('planted')\n")
    elif change == "mode":
        os.chmod(main, 0o644)
    elif change == "added":
        writable(copy / "sandbox" / "json.py")
        (copy / "sandbox" / "json.py").write_text("print('planted')\n")
    elif change == "removed":
        writable(copy / "sandbox" / "helper.py")
        (copy / "sandbox" / "helper.py").unlink()
    elif change == "replaced":  # same content, another file
        writable(main)
        data = main.read_bytes()
        main.unlink()
        main.write_bytes(data)
        os.chmod(main, 0o444)
    elif change == "marker":
        (copy / boot.MARKER).chmod(0o600)
        (copy / boot.MARKER).write_text("{}")
    elif change == "top":  # beside the tree
        os.chmod(copy, 0o700)
        (copy / "sitecustomize.py").write_text("print('planted')\n")
    else:
        pyc = next((copy / "sandbox" / "__pycache__").glob("helper.*.pyc"))
        writable(pyc)
        pyc.write_bytes(pyc.read_bytes())  # rewritten in place
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and result.stdout == ""
    assert "refused, nothing run" in result.stderr and f"rm -rf '{copy}'" in result.stderr


def test_an_incomplete_copy_is_extracted_again(pyz, tmp_path):
    """rm -rf ~/.cache removes the record at the top, then stops at the read-only folders: not a changed copy."""
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    os.chmod(copy, 0o700)
    (copy / boot.MARKER).unlink()
    (copy / "PACKAGING").unlink()
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 0 and "was incomplete" in result.stderr
    assert json.loads(result.stdout)["argv"][1:] == ["x"]
    assert copies(tmp_path) == [copy] and (copy / boot.MARKER).is_file()
    assert stat.S_IMODE(copy.stat().st_mode) == 0o500
    assert not [p for p in copy.parent.iterdir() if p.name.startswith(".")]


def test_a_removed_copy_is_extracted_again(pyz, tmp_path):
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    boot.remove_tree(str(copy))
    assert run(pyz, tmp_path, "x").returncode == 0
    assert len(copies(tmp_path)) == 1


def test_a_file_that_does_not_match_the_manifest_is_never_extracted(tmp_path):
    bundle = fake_bundle(tmp_path / "bundle.tar.gz")
    files, _ = make_pyz.bundle_tree(bundle)
    files["PACKAGING"] = (b"pyz\n", 0o644)
    manifest = make_pyz.manifest(files)
    files["sandbox/helper.py"] = (b"import os; os.system('planted')\n", 0o644)  # the tree changed after the manifest
    pyz = make_pyz.write_pyz(tmp_path / "bad.pyz", files, manifest, 0)
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and "damaged: sandbox/helper.py does not match its manifest" in result.stderr
    assert copies(tmp_path) == []
    assert not [p for p in (tmp_path / "cache/sandbox-ai/pyz").iterdir()]  # its temporary folder is gone too


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd", "a//b", "a/./b", "", "a\\b"])
@pytest.mark.parametrize("where", ["files", "compile", "home"])
def test_paths_outside_the_tree_are_refused(name, where):
    data = {"format": 1, "kind": "pyz", "version": "1.0.0", "files": [["a", "0" * 64, 0, 0o644]], "links": []}
    if where == "files":
        data["files"][0][0] = name
    elif where == "compile":
        data["compile"] = [name]
    else:
        data["home"] = name or "/"
    with pytest.raises(boot.BootError, match="bad path"):
        boot.load_manifest(json.dumps(data).encode())


@pytest.mark.parametrize("target", ["../../../etc", "/etc", "../.."])
def test_links_out_of_the_tree_are_refused(target):
    raw = json.dumps({"format": 1, "kind": "appimage", "version": "1.0.0", "files": [],
                      "links": [["python/bin/x", target]]}).encode()
    if target == "../..":  # python/bin/../.. is the top of the tree itself: allowed
        boot.load_manifest(raw)
        return
    with pytest.raises(boot.BootError, match="points outside"):
        boot.load_manifest(raw)


def test_the_cache_is_never_used_through_a_link(pyz, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "sandbox-ai").symlink_to(elsewhere)
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and "is a link or not a folder" in result.stderr
    assert list(elsewhere.iterdir()) == []
    (tmp_path / "cache" / "sandbox-ai").unlink()
    (tmp_path / "cache" / "sandbox-ai").mkdir()
    (tmp_path / "cache" / "sandbox-ai" / "pyz").symlink_to(elsewhere)
    assert run(pyz, tmp_path, "x").returncode == 1 and list(elsewhere.iterdir()) == []


def test_a_copy_that_is_a_link_is_refused(pyz, tmp_path):
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    moved = tmp_path / "moved"
    os.chmod(copy, 0o700)  # a folder moved elsewhere gets a new "..": it must be writable
    os.rename(copy, moved)
    copy.symlink_to(moved)
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and "is a link" in result.stderr


def test_python_variables_never_reach_the_start(pyz, tmp_path):
    """./sandbox-ai.pyz runs without -I: a PYTHONPATH with a planted json.py must not be imported."""
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "json.py").write_text(f"open({str(tmp_path / 'ran')!r}, 'w').close()\n")
    result = run(pyz, tmp_path, "x", PYTHONPATH=str(planted))
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "ran").exists()
    assert json.loads(result.stdout)["isolated"] == 1


def test_concurrent_first_starts_share_one_copy(pyz, tmp_path):
    results = []
    threads = [threading.Thread(target=lambda: results.append(run(pyz, tmp_path, "x"))) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [r.returncode for r in results] == [0, 0, 0, 0], [r.stderr for r in results]
    assert len(copies(tmp_path)) == 1
    assert not [p for p in (tmp_path / "cache/sandbox-ai/pyz").iterdir() if p.name.startswith(".")]


def test_stale_temporary_folders_are_removed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    old = root / ".1.0-abc.x1.tmp"
    (old / "sub").mkdir(parents=True)
    os.chmod(old / "sub", 0o500)
    fresh = root / ".1.0-abc.x2.tmp"
    fresh.mkdir()
    os.utime(old, (0, 0))
    boot.remove_stale(str(root), "1.0-abc")
    assert not old.exists() and fresh.exists()


def test_a_damaged_archive_is_reported(tmp_path, pyz):
    with zipfile.ZipFile(pyz) as archive:
        info = archive.getinfo("tree/sandbox/main.py")
    data = bytearray(pyz.read_bytes())
    start = info.header_offset + 30 + len(info.filename.encode())  # zipfile counts the shell line in
    for offset in range(start + 2, start + info.compress_size - 2):  # the compressed data, not its header
        data[offset] ^= 0x55
    pyz.write_bytes(bytes(data))
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and "is damaged" in result.stderr and "Traceback" not in result.stderr
    assert copies(tmp_path) == []


# -- the AppImage's start: the same boot, from a folder --

def appimage_package(tmp_path):
    """usr/lib/sandbox-ai of an AppImage: boot.py, manifest.json, tree/ (python/, sandbox-ai/) and pyside/."""
    base = tmp_path / "AppDir" / "usr" / "lib" / "sandbox-ai"
    tree = base / "tree"
    (tree / "python" / "bin").mkdir(parents=True)
    (tree / "python" / "bin" / "python3.13").write_text(f"#!/bin/sh\nexec {sys.executable} \"$@\"\n")
    os.chmod(tree / "python" / "bin" / "python3.13", 0o755)
    (tree / "python" / "bin" / "python3").symlink_to("python3.13")
    (tree / "sandbox-ai" / "sandbox").mkdir(parents=True)
    (tree / "sandbox-ai" / "sandbox" / "agent_sandbox.py").write_text('__version__ = "9.8.7"\n')
    (tree / "sandbox-ai" / "sandbox" / "main.py").write_bytes(FAKE_MAIN)
    (tree / "sandbox-ai" / "projects" / "sandbox-ai-app" / "sandbox_ai_app").mkdir(parents=True)
    (tree / "sandbox-ai" / "PACKAGING").write_text("appimage\n")
    (base / "pyside" / "PySide6").mkdir(parents=True)
    (base / "boot.py").write_bytes((ROOT / "scripts/dist_boot.py").read_bytes())
    assert make_pyz.main(["--manifest", "--tree", str(tree), "--kind", "appimage", "--home", "sandbox-ai",
                          "--python", "python/bin/python3", "--out", str(base / "manifest.json")]) == 0
    return base


def start_appimage(base, tmp_path, *args, argv0=None):
    env = env_for(tmp_path, APPIMAGE=str(tmp_path / "sandbox-ai-x86_64.AppImage"),
                  **({"ARGV0": argv0} if argv0 else {}))
    return subprocess.run([sys.executable, "-I", str(base / "boot.py"), *args], capture_output=True, text=True,
                          timeout=120, env=env)


def test_the_appimage_runs_the_command_line_from_its_copy(tmp_path):
    base = appimage_package(tmp_path)
    for result in (start_appimage(base, tmp_path, "cli", "--version"),
                   start_appimage(base, tmp_path, "--version", argv0="/home/u/.local/bin/sandbox-ai")):
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        assert report["argv"][1:] == ["--version"] and report["isolated"] == 1
        copy = Path(report["file"]).parents[2]
        assert copy.parent == tmp_path / "cache" / "sandbox-ai" / "appimage"
        assert copy.name.startswith("9.8.7-") and "cpython" not in copy.name  # compiled when it was built
    assert os.readlink(copy / "python" / "bin" / "python3") == "python3.13"  # links stay links


def test_the_appimage_starts_the_app_with_pyside_from_inside(tmp_path):
    base = appimage_package(tmp_path)
    source = boot.DirSource(str(base))
    raw = source.manifest()
    manifest = boot.load_manifest(raw)
    top = boot.ensure_copy(source, manifest, hashlib.sha256(raw).hexdigest(), env_for(tmp_path))
    environ = {"APPIMAGE": "/apps/sandbox-ai-x86_64.AppImage"}
    executable, argv = boot.command_line(manifest, top, ["--hidden"], source, environ, "sandbox-ai-x86_64.AppImage")
    assert executable == os.path.join(top, "python", "bin", "python3")
    assert argv[:3] == [executable, "-I", "-c"] and argv[3] == boot.APP_STARTER
    assert argv[4:] == [str(base / "pyside"), os.path.join(top, "sandbox-ai", "projects", "sandbox-ai-app"), "--hidden"]
    assert environ["SANDBOX_AI_HOME"] == os.path.join(top, "sandbox-ai")
    assert environ["SANDBOX_AI_APP_PACKAGE_LAUNCHER"] == "/apps/sandbox-ai-x86_64.AppImage"
    environ = {"SANDBOX_AI_HOME": "/my/checkout"}  # yours wins
    _, argv = boot.command_line(manifest, top, ["app", "--debug"], source, environ, None)
    assert environ["SANDBOX_AI_HOME"] == "/my/checkout" and argv[-1] == "--debug" and "app" not in argv[6:]
    _, argv = boot.command_line(manifest, top, ["cli", "list"], source, {}, None)
    assert argv[-2:] == [os.path.join(top, "sandbox-ai", "sandbox", "main.py"), "list"]


def test_a_copy_that_cannot_be_executed_is_reported(pyz, tmp_path, capsys):
    """A cache on a noexec file system: a message, not a traceback."""
    def refuse(executable, argv, environ):
        raise PermissionError(13, "Permission denied")
    raw = zipfile.ZipFile(pyz).read("manifest.json")
    source = boot.ZipSource(str(pyz))
    boot.ensure_copy(source, boot.load_manifest(raw), hashlib.sha256(raw).hexdigest(), env_for(tmp_path))
    original = boot.package_source
    boot.package_source = lambda: source
    try:
        assert boot.main(["x"], env_for(tmp_path), execv=refuse) == 1
    finally:
        boot.package_source = original
    assert "noexec" in capsys.readouterr().err


def test_every_start_reads_the_manifest_without_zipfile(pyz, tmp_path):
    """The manifest is stored uncompressed as the second entry: read directly, it is the same bytes zipfile reads;
    an archive laid out otherwise falls back to zipfile."""
    with zipfile.ZipFile(pyz) as archive:
        assert archive.getinfo("manifest.json").compress_type == zipfile.ZIP_STORED
        assert boot.stored_manifest(str(pyz)) == archive.read("manifest.json")
    other = tmp_path / "other.pyz"
    with zipfile.ZipFile(other, "w") as archive:
        archive.writestr("__main__.py", "")
        archive.writestr("manifest.json", "{}", compress_type=zipfile.ZIP_DEFLATED)
    assert boot.stored_manifest(str(other)) is None
    assert boot.ZipSource(str(other)).manifest() == b"{}"
    assert run(pyz, tmp_path, "x").returncode == 0
    result = subprocess.run([sys.executable, "-I", "-X", "importtime", str(pyz), "x"], capture_output=True,
                            text=True, timeout=60, env=env_for(tmp_path))
    assert result.returncode == 0
    imported = [line.split("|")[-1].strip() for line in result.stderr.splitlines() if line.startswith("import time:")]
    assert "json" in imported and "zipfile" not in imported  # the start itself (main.py runs without -X importtime)


def test_a_workspace_holding_the_pyz_is_refused(tmp_path):
    """Review finding (HIGH), reproduced: `curl -fLO .../sandbox-ai.pyz` into a project, then a start there: the
    agent could rewrite the .pyz, and its next start runs that on the host. The boot names the package and its copy
    (SANDBOX_AI_PACKAGE, SANDBOX_AI_PACKAGE_COPY), the launcher refuses such a workspace."""
    make_dist = load("make_dist")
    project = tmp_path / "project"
    project.mkdir()
    pyz = make_pyz.build_pyz(make_dist.bundle(tmp_path, VERSION), project / "sandbox-ai.pyz", epoch=0)
    result = run(pyz, tmp_path, "claude", "--dry-run", "--no-config", "-w", str(project))
    assert result.returncode != 0 and "contains" in result.stderr and "sandbox-ai.pyz" in result.stderr, \
        result.stdout[-500:] + result.stderr[-500:]
    elsewhere = tmp_path / "bin"
    elsewhere.mkdir()
    moved = pyz.rename(elsewhere / "sandbox-ai.pyz")
    result = run(moved, tmp_path, "claude", "--dry-run", "--no-config", "-w", str(project))
    assert result.returncode == 0, result.stderr[-1000:]
    assert run(moved, tmp_path, "claude", "--dry-run", "--no-config", "-w", str(elsewhere)).returncode != 0


def test_the_boot_names_the_package_and_its_copy(pyz, tmp_path):
    report_main = b"""import json, os, sys
print(json.dumps({k: os.environ.get(k) for k in ("SANDBOX_AI_PACKAGE", "SANDBOX_AI_PACKAGE_COPY")}))
"""
    bundle = fake_bundle(tmp_path / "b.tar.gz", {"sandbox/main.py": (report_main, 0o644)})
    built = make_pyz.build_pyz(bundle, tmp_path / "x.pyz")
    result = run(built, tmp_path, "x", SANDBOX_AI_PACKAGE="/elsewhere/planted")
    [copy] = copies(tmp_path)
    assert json.loads(result.stdout) == {"SANDBOX_AI_PACKAGE": os.path.realpath(built),
                                         "SANDBOX_AI_PACKAGE_COPY": str(copy)}


@pytest.mark.parametrize("mode", [0o777, 0o770, 0o707])
def test_the_appimage_boot_refuses_a_folder_others_can_write(tmp_path, mode):
    """The boot checks the AppImage's folder (and its parents) again before it copies anything from it."""
    base = appimage_package(tmp_path)
    assert start_appimage(base, tmp_path, "cli", "x").returncode == 0
    (tmp_path / "AppDir" / "usr").chmod(mode)
    result = start_appimage(base, tmp_path, "cli", "x")
    assert result.returncode == 1 and "writable by others" in result.stderr
    (tmp_path / "AppDir" / "usr").chmod(0o755)
    assert boot.check_package_dir(str(base)) is None


def test_a_copy_in_a_tree_sandboxes_could_write_is_refused(pyz, tmp_path):
    """Review finding: the copy's record certifies itself, so it must lie where no sandbox writes. A cache inside a
    tree sandbox-ai has exposed (its exposed.json: workspaces, rw mounts, --state-dir of earlier runs) is refused."""
    state = tmp_path / "state" / "sandbox-ai"
    state.mkdir(parents=True)
    (state / "exposed.json").write_text(json.dumps([str(tmp_path / "unrelated"), str(tmp_path)]))
    result = run(pyz, tmp_path, "x", XDG_STATE_HOME=str(tmp_path / "state"))
    assert result.returncode == 1 and "a sandbox could write" in result.stderr and result.stdout == ""
    assert copies(tmp_path) == []
    (state / "exposed.json").write_text(json.dumps([str(tmp_path / "unrelated"), str(tmp_path / "cache" / "sandbox-ai" / "pyz" / "x")]))
    assert run(pyz, tmp_path, "x", XDG_STATE_HOME=str(tmp_path / "state")).returncode == 1  # a tree inside it, too
    (state / "exposed.json").write_text(json.dumps([str(tmp_path / "unrelated")]))
    assert run(pyz, tmp_path, "x", XDG_STATE_HOME=str(tmp_path / "state")).returncode == 0


def test_a_cache_others_can_write_is_refused_or_made_private(pyz, tmp_path):
    """Review finding: the cache base (XDG_CACHE_HOME) writable by others (not a sticky folder of root's) is refused;
    sandbox-ai's own folder there loses group and other write."""
    (tmp_path / "cache").mkdir(mode=0o700)
    (tmp_path / "cache").chmod(0o777)
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 1 and "writable by others" in result.stderr and copies(tmp_path) == []
    (tmp_path / "cache").chmod(0o700)
    (tmp_path / "cache" / "sandbox-ai").mkdir()
    (tmp_path / "cache" / "sandbox-ai").chmod(0o777)
    assert run(pyz, tmp_path, "x").returncode == 0
    assert stat.S_IMODE((tmp_path / "cache" / "sandbox-ai").stat().st_mode) & 0o022 == 0


def test_a_sticky_root_cache_base_is_fine(pyz, tmp_path):
    base = tmp_path / "shared"
    base.mkdir()
    base.chmod(0o1777)
    if base.stat().st_uid != 0:
        pytest.skip("needs a folder of root's")
    assert run(pyz, tmp_path, "x", XDG_CACHE_HOME=str(base)).returncode == 0


def other_version(tmp_path, version):
    bundle = fake_bundle(tmp_path / f"{version}.tar.gz", {"sandbox/agent_sandbox.py": (f'__version__ = "{version}"\n'.encode(), 0o644)})
    return make_pyz.build_pyz(bundle, tmp_path / f"{version}.pyz")


@pytest.mark.skipif(not os.path.isdir("/proc/self"), reason="the cleanup looks for users of a copy in /proc")
def test_copies_of_other_versions_go_once_nothing_runs_from_them(pyz, tmp_path):
    """Review finding: every version left its copy behind (about 11 MB per .pyz, 68 MB per AppImage)."""
    old = other_version(tmp_path, "9.8.6")
    assert run(old, tmp_path, "x").returncode == 0
    [old_copy] = copies(tmp_path)
    busy = subprocess.Popen(["sleep", "30"], cwd=old_copy)  # something still runs from it
    try:
        assert run(pyz, tmp_path, "x").returncode == 0
        assert old_copy in copies(tmp_path) and len(copies(tmp_path)) == 2
    finally:
        busy.kill()
        busy.wait()
    assert run(pyz, tmp_path, "x").returncode == 0
    [current] = copies(tmp_path)
    assert current != old_copy and current.name.startswith("9.8.7-")
    assert not [p for p in current.parent.iterdir() if p.name.startswith(".")]


def test_the_same_package_for_another_python_stays(pyz, tmp_path):
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    twin = copy.with_name(copy.name.rsplit("-", 1)[0] + "-cpython-399")  # this package, unpacked by another Python
    twin.mkdir()
    assert run(pyz, tmp_path, "x").returncode == 0
    assert twin in copies(tmp_path)


def test_an_unreadable_record_is_extracted_again(pyz, tmp_path):
    """Review finding: a record cut short (a crash, a full disk) is like a missing one, not a changed copy."""
    assert run(pyz, tmp_path, "x").returncode == 0
    [copy] = copies(tmp_path)
    (copy / boot.MARKER).chmod(0o600)
    (copy / boot.MARKER).write_text('{"digest": "ab')
    result = run(pyz, tmp_path, "x")
    assert result.returncode == 0 and "was incomplete" in result.stderr, result.stderr
    assert json.loads((copy / boot.MARKER).read_text())["entries"]


def test_the_copy_is_on_disk_before_it_is_renamed_into_place(pyz, tmp_path, monkeypatch):
    """Review finding: without fsync a crash could leave a renamed copy with empty files and a valid record."""
    synced = []
    real = os.fsync

    def fsync(fd):
        synced.append(os.readlink(f"/proc/self/fd/{fd}") if os.path.exists("/proc/self/fd") else fd)
        return real(fd)
    monkeypatch.setattr(boot.os, "fsync", fsync)
    raw = zipfile.ZipFile(pyz).read("manifest.json")
    manifest = boot.load_manifest(raw)
    top = boot.ensure_copy(boot.ZipSource(str(pyz)), manifest, hashlib.sha256(raw).hexdigest(), env_for(tmp_path))
    if os.path.exists("/proc/self/fd"):
        names = {os.path.basename(str(p)) for p in synced}
        assert {"main.py", "helper.py", boot.MARKER} <= names  # the files and the record
        assert any(name.endswith(".pyc") for name in names)  # what compileall wrote, too
        assert str(Path(top).parent) in synced  # the folder that holds the renamed copy
    assert len(synced) >= len(manifest["files"]) + 2


def test_two_starts_setting_an_incomplete_copy_aside_both_go_on(pyz, tmp_path, monkeypatch):
    """Review finding: both starts find the copy incomplete and rename it aside; the second rename finds nothing
    (FileNotFoundError) and must carry on, not end with 'cannot start'."""
    raw = zipfile.ZipFile(pyz).read("manifest.json")
    manifest, digest = boot.load_manifest(raw), hashlib.sha256(raw).hexdigest()
    source = boot.ZipSource(str(pyz))
    top = boot.ensure_copy(source, manifest, digest, env_for(tmp_path))
    os.chmod(top, 0o700)
    os.unlink(os.path.join(top, boot.MARKER))
    real = os.rename

    def rename(src, dst):
        if src == top and dst.endswith(".tmp"):  # the other start was first
            real(src, dst + ".other")
            boot.remove_tree(dst + ".other")
            raise FileNotFoundError(2, "No such file or directory", src)
        return real(src, dst)
    monkeypatch.setattr(boot.os, "rename", rename)
    assert boot.ensure_copy(source, manifest, digest, env_for(tmp_path)) == top
    assert boot.verify(top, digest, manifest) is None


def test_the_pyz_does_not_depend_on_the_zlib_build(pyz):
    """Review finding: deflate output at level 9 differs between zlib and zlib-ng (distributions ship either), so
    the same bundle gave another .pyz on another machine. Every entry is stored."""
    with zipfile.ZipFile(pyz) as archive:
        assert {info.compress_type for info in archive.infolist()} == {zipfile.ZIP_STORED}
