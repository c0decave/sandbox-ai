"""The PyPI package sandbox-ai-app: one wheel with the command line (sandbox_ai, the web interface in it) and the desktop
app (sandbox_ai_app), the app's PySide6 as the extra [gui]. Built here as the release builds it (python -m build
--no-isolation from the release files, offline), installed into a fresh virtual environment without dependencies:
the commands run from there, the app finds the command line of its own installation, doctor and hosts say "pip"."""
import datetime
import email.parser
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIST = "sandbox-ai-app"
MIN_AGE = datetime.timedelta(days=14)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def version():
    return re.search(r'^__version__ = "([^"]+)"$', (ROOT / "sandbox/agent_sandbox.py").read_text(), re.M)[1]


def pyproject():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    return tomllib.loads((ROOT / "pyproject.toml").read_text())


def test_the_distribution_is_sandbox_ai_app_with_both_commands_and_the_gui_extra():
    project = pyproject()["project"]
    assert project["name"] == DIST
    assert project["requires-python"] == ">=3.9"
    assert "dependencies" not in project or project["dependencies"] == []  # the command line: standard library only
    assert project["scripts"] == {"sandbox-ai": "sandbox_ai.cli:main", "sandbox-ai-app": "sandbox_ai_app.launch:main"}
    assert set(project["optional-dependencies"]) == {"gui"}
    tool = pyproject()["tool"]["setuptools"]
    assert tool["package-dir"] == {"sandbox_ai": "sandbox", "sandbox_ai_app": "projects/sandbox-ai-app/sandbox_ai_app"}
    assert tool["packages"] == ["sandbox_ai", "sandbox_ai_app"]
    tomllib = pytest.importorskip("tomllib")
    own = tomllib.loads((ROOT / "projects/sandbox-ai-app/pyproject.toml").read_text())
    assert tool["package-data"]["sandbox_ai_app"] == own["tool"]["setuptools"]["package-data"]["sandbox_ai_app"]


def test_the_gui_extra_pins_pyside6_exactly_and_old_enough():
    """Exact pins only, each released at least 14 days before it was pinned (the comment beside it says when); for
    Python 3.12+ the release the AppImage and the macOS app carry (requirements-app.txt); below 3.12 that release
    crashes ("deallocating None"), so those Pythons get the releases the app's uv.lock resolves for them."""
    gui = pyproject()["project"]["optional-dependencies"]["gui"]
    text = (ROOT / "pyproject.toml").read_text()
    pinned_on = re.search(r"# The \[gui\] pins: pinned on (\d{4}-\d{2}-\d{2})", text)
    assert pinned_on, "the date the pins were made"
    pinned_on = datetime.date.fromisoformat(pinned_on[1])
    assert pinned_on <= datetime.date.today()
    seen = {}
    for requirement in gui:
        match = re.fullmatch(r"(PySide6-Essentials|shiboken6)==([\d.]+); (.+)", requirement)
        assert match, requirement
        name, release, marker = match.groups()
        uploaded = re.search(rf"# {re.escape(name)} {re.escape(release)}: uploaded (\d{{4}}-\d{{2}}-\d{{2}})", text)
        assert uploaded, f"{requirement}: its upload date beside it"
        assert datetime.date.fromisoformat(uploaded[1]) <= pinned_on - MIN_AGE, requirement
        seen.setdefault(marker, set()).add((name.lower(), release))
    app_pins = set(re.findall(r"^(pyside6-essentials|shiboken6)==([\d.]+)",
                              (ROOT / "packaging/requirements-app.txt").read_text(), re.M))
    assert seen["python_version >= '3.12'"] == app_pins
    lock = (ROOT / "projects/sandbox-ai-app/uv.lock").read_text()
    below = [marker for marker in seen if marker != "python_version >= '3.12'"]
    assert below and all(release != "6.12.0" for marker in below for _, release in seen[marker])
    for marker in below:
        for _, release in seen[marker]:
            assert f'name = "pyside6-essentials"\nversion = "{release}"' in lock, release


def test_the_web_interface_and_the_app_files_are_named_for_the_sdist_too():
    manifest = (ROOT / "MANIFEST.in").read_text()
    assert "graft sandbox/web" in manifest
    assert "graft projects/sandbox-ai-app/sandbox_ai_app" in manifest


APP_PACKAGE = ROOT / "projects/sandbox-ai-app/sandbox_ai_app"


def board_icons():
    """The Board's icons (sandbox_ai_app/icons/board/KIND.svg), relative to the app's package, and the kinds of part
    the board knows (board.py's KINDS)."""
    kinds = set(re.findall(r'^\s*Kind\("([a-z0-9_]+)"', (APP_PACKAGE / "board.py").read_text(), re.M))
    icons = sorted(p.relative_to(APP_PACKAGE).as_posix() for p in (APP_PACKAGE / "icons/board").glob("*.svg"))
    return kinds, icons


def test_the_board_icons_ship_in_the_wheel_the_sdist_and_the_bundle():
    """Every kind of part has its icon, and the icons are package data of the wheel (both pyproject.toml's), in the
    sdist (MANIFEST.in's graft) and among the release files the bundle - and so the AppImage and the macOS app,
    which carry the bundle's tree - is made of."""
    import fnmatch
    kinds, icons = board_icons()
    assert len(kinds) >= 23 and {f"icons/board/{kind}.svg" for kind in kinds} <= set(icons), sorted(kinds)
    globs = pyproject()["tool"]["setuptools"]["package-data"]["sandbox_ai_app"]
    assert all(any(fnmatch.fnmatch(icon, glob) for glob in globs) for icon in icons), globs
    assert "graft projects/sandbox-ai-app/sandbox_ai_app" in (ROOT / "MANIFEST.in").read_text()
    make_dist = load("make_dist_icons", ROOT / "scripts/make_dist.py")
    released = set(make_dist.release_files())
    missing = [icon for icon in icons if f"projects/sandbox-ai-app/sandbox_ai_app/{icon}" not in released]
    assert not missing, missing


def test_the_release_publishes_exactly_this_wheel_and_sdist():
    release = (ROOT / ".github/workflows/release.yml").read_text()
    assert "cp dist/sandbox_ai_app-*.whl dist/sandbox_ai_app-*.tar.gz pypi/" in release
    assert "dist/sandbox_ai-*" not in release and "cp dist/*.whl" not in release
    assert "environment: pypi" in release and "id-token: write" in release
    notes = load("release_notes_pypi", ROOT / "scripts/release_notes.py")
    names = [name for name, _ in notes.files("1.2.3")]
    assert "sandbox_ai_app-1.2.3-py3-none-any.whl" in names and "sandbox_ai_app-1.2.3.tar.gz" in names
    assert not [name for name in names if name.startswith("sandbox_ai-")]
    text = notes.notes("1.2.3")
    for needle in ("pipx install sandbox-ai-app", "pip install 'sandbox-ai-app[gui]'", "pipx upgrade sandbox-ai-app"):
        assert needle in text, needle
    assert not re.search(r"(pip|pipx) (install|upgrade)( -U| --user)? sandbox-ai(?![-\w])", text)


@pytest.mark.parametrize("path", ["README.md", "README_DE.md", "RELEASING.md", "sandbox/main.py", "sandbox/hosts.py",
                                  "sandbox/web/help.json", "scripts/release_notes.py",
                                  "projects/sandbox-ai-app/sandbox_ai_app/help_content.py",
                                  "projects/sandbox-ai-app/sandbox_ai_app/ui.py",
                                  "projects/sandbox-ai-app/sandbox_ai_app/i18n.py",
                                  "projects/sandbox-ai-app/README.md", "docs/video/storyboard.py"])
def test_no_text_sends_anyone_to_the_pypi_name_that_is_not_ours(path):
    """PyPI's "sandbox-ai" is not this project: `pip install sandbox-ai` would install someone else's code."""
    text = (ROOT / path).read_text(encoding="utf-8")
    assert not re.search(r"(pip|pipx) (install|upgrade)( -U| --user| --upgrade)? sandbox-ai(?![-\w])", text), path


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """(wheel, sdist) built from the release files as make_dist.py builds them, without the network."""
    pytest.importorskip("build")
    pytest.importorskip("setuptools")
    make_dist = load("make_dist_pypi", ROOT / "scripts/make_dist.py")
    tree = tmp_path_factory.mktemp("tree")
    for name in make_dist.release_files():
        target = tree / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, target)
    out = tmp_path_factory.mktemp("out")
    done = subprocess.run([sys.executable, "-m", "build", "--sdist", "--wheel", "--no-isolation", "--outdir", str(out),
                           str(tree)], capture_output=True, text=True, timeout=600)
    assert done.returncode == 0, done.stdout[-3000:] + done.stderr[-3000:]
    wheels, sdists = sorted(out.glob("*.whl")), sorted(out.glob("*.tar.gz"))
    assert [w.name for w in wheels] == [f"sandbox_ai_app-{version()}-py3-none-any.whl"]
    assert [s.name for s in sdists] == [f"sandbox_ai_app-{version()}.tar.gz"]
    return wheels[0], sdists[0]


def test_the_wheel_holds_the_command_line_the_web_interface_and_the_app(built):
    wheel, _ = built
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        info = f"sandbox_ai_app-{version()}.dist-info"
        metadata = email.parser.Parser().parsestr(archive.read(f"{info}/METADATA").decode())
        entry_points = archive.read(f"{info}/entry_points.txt").decode()
    assert metadata["Name"] == DIST and metadata["Version"] == version()
    assert metadata["Requires-Python"] == ">=3.9"
    assert metadata.get_all("Provides-Extra") == ["gui"]
    requires = metadata.get_all("Requires-Dist")
    assert requires and all('extra == "gui"' in r for r in requires), "nothing without [gui]"
    assert any(r.startswith("pyside6-essentials==6.12.0") or r.startswith("PySide6-Essentials==6.12.0")
               for r in requires)
    assert "sandbox-ai = sandbox_ai.cli:main" in entry_points
    assert "sandbox-ai-app = sandbox_ai_app.launch:main" in entry_points
    for name in ("sandbox_ai/cli.py", "sandbox_ai/main.py", "sandbox_ai/agent_sandbox.py", "sandbox_ai/webui.py",
                 "sandbox_ai/web/help.json", "sandbox_ai/runtime.py",
                 "sandbox_ai_app/__init__.py", "sandbox_ai_app/__main__.py", "sandbox_ai_app/launch.py",
                 "sandbox_ai_app/main.py", "sandbox_ai_app/backend.py", "sandbox_ai_app/i18n.py",
                 "sandbox_ai_app/help_content.py", "sandbox_ai_app/web/terminal.html",
                 "sandbox_ai_app/web/terminal.js"):
        assert name in names, name
    web = {n for n in names if n.startswith("sandbox_ai/web/")}
    assert web >= {f"sandbox_ai/{p.relative_to(ROOT / 'sandbox').as_posix()}"
                   for p in (ROOT / "sandbox/web").iterdir() if p.is_file()}
    app = {p.relative_to(ROOT / "projects/sandbox-ai-app").as_posix()
           for p in (ROOT / "projects/sandbox-ai-app/sandbox_ai_app").glob("*.py")}
    assert app <= names, sorted(app - names)[:5]
    kinds, icons = board_icons()
    assert {f"sandbox_ai_app/icons/board/{kind}.svg" for kind in kinds} <= names
    assert {f"sandbox_ai_app/{icon}" for icon in icons} <= names, "every Board icon is package data"
    assert not [n for n in names if n.startswith(("tests/", "projects/", "sandbox/"))]


RUNTIME = (("sandbox/", "sandbox_ai/"), ("projects/sandbox-ai-app/sandbox_ai_app/", "sandbox_ai_app/"))
# Files added since 0.1.188 that the program reads at run time; package-data's patterns are lists, so a new kind of
# file (a JSON beside the code, a font, a subfolder of web/) is easily left out of the wheel while the checkout and
# the bundle work.
RUNTIME_SINCE_0_1_188 = (
    "sandbox/cairn_workers.json", "sandbox/live_evidence.json", "sandbox/model_evidence.json",
    "sandbox/toolchains.py", "sandbox/pid1.py", "sandbox/hostinit.py", "sandbox/fixes.py", "sandbox/llm_share.py",
    "sandbox/mcp_build.py", "sandbox/session_state.py", "sandbox/cairn_workers.py", "sandbox/web/help.json",
    "sandbox/web/fonts/fonts.json", "sandbox/web/fonts/Lexend-Regular.woff2", "sandbox/web/fonts/OFL-Lexend.txt",
    "sandbox/presets/android-development.json", "sandbox/presets/android-development.rules.json",
    "sandbox/presets/pair-reviewer.json", "sandbox/presets/docs-from-code.json",
    "sandbox/recipes/android-dev/sdk.lock", "sandbox/recipes/android-dev/install.sh",
    "sandbox/recipes/android-dev/android-emulator", "sandbox/recipes/android-dev/android-sdk-license",
    "sandbox/recipes/android-dev/package-xml/platform-tools.xml",
    "projects/sandbox-ai-app/sandbox_ai_app/typefaces/fonts.json",
    "projects/sandbox-ai-app/sandbox_ai_app/typefaces/OpenDyslexic-Regular.otf",
    "projects/sandbox-ai-app/sandbox_ai_app/typefaces/AtkinsonHyperlegibleNext-Regular.ttf",
    "projects/sandbox-ai-app/sandbox_ai_app/typefaces/OFL-OpenDyslexic.txt",
    "projects/sandbox-ai-app/sandbox_ai_app/icons/board/toolchains.svg",
    "projects/sandbox-ai-app/sandbox_ai_app/reading_fonts.py", "projects/sandbox-ai-app/sandbox_ai_app/help_pages.py")


def runtime_files():
    """Every release file of the two packages (make_dist.py's list: what git tracks), with its name in the wheel."""
    make_dist = load("make_dist_runtime", ROOT / "scripts/make_dist.py")
    found = {}
    for name in make_dist.release_files():
        for prefix, package in RUNTIME:
            if name.startswith(prefix) and "/__pycache__/" not in name:
                found[name] = package + name[len(prefix):]
    return found


def test_the_runtime_files_since_0_1_188_are_release_files():
    files = runtime_files()
    assert set(RUNTIME_SINCE_0_1_188) <= set(files), sorted(set(RUNTIME_SINCE_0_1_188) - set(files))


def test_the_wheel_holds_every_file_of_both_packages(built):
    """Not only the code: every file of sandbox/ and the app's package that a release ships is in the wheel too."""
    wheel, _ = built
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    missing = sorted(name for name, inside in runtime_files().items() if inside not in names)
    # pyproject.toml's package-data, and MANIFEST.in, which setuptools includes in the wheel too
    assert not missing, f"{len(missing)} files not in the wheel: {missing[:20]}"


def test_the_sdist_holds_every_file_of_both_packages(built):
    _, sdist = built
    import tarfile
    with tarfile.open(sdist) as archive:
        names = {member.name.split("/", 1)[1] for member in archive.getmembers() if "/" in member.name}
    missing = sorted(set(runtime_files()) - names)
    assert not missing, f"{len(missing)} files not in the sdist (MANIFEST.in): {missing[:20]}"


def test_the_sdist_builds_the_same_wheel(built):
    """The wheel above is built from the sdist (python -m build): the app is in it, its tests are not needed."""
    _, sdist = built
    import tarfile
    with tarfile.open(sdist) as archive:
        names = {member.name.split("/", 1)[1] for member in archive.getmembers() if "/" in member.name}
    assert {"pyproject.toml", "MANIFEST.in", "sandbox/webui.py", "projects/sandbox-ai-app/sandbox_ai_app/main.py",
            "projects/sandbox-ai-app/sandbox_ai_app/web/terminal.html"} <= names
    assert {f"projects/sandbox-ai-app/sandbox_ai_app/{icon}" for icon in board_icons()[1]} <= names


@pytest.fixture(scope="module")
def installed(built, tmp_path_factory):
    """A fresh virtual environment with the wheel installed, without its dependencies (no PySide6) and offline."""
    venv = tmp_path_factory.mktemp("venv") / "env"
    made = subprocess.run([sys.executable, "-m", "venv", str(venv)], capture_output=True, text=True, timeout=300)
    if made.returncode != 0:
        pytest.skip(f"no venv with pip here: {made.stderr[-500:]}")
    done = subprocess.run([str(venv / "bin/python"), "-m", "pip", "install", "--no-deps", "--no-index",
                           "--disable-pip-version-check", "-q", str(built[0])], capture_output=True, text=True,
                          timeout=600)
    assert done.returncode == 0, done.stderr[-3000:]
    return venv


def clean_env(tmp_path, venv):
    env = {key: value for key, value in os.environ.items()
           if key not in ("SANDBOX_AI_HOME", "PYTHONPATH", "VIRTUAL_ENV", "PYTHONHOME")}
    env.update(HOME=str(tmp_path / "home"), XDG_STATE_HOME=str(tmp_path / "state"),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_DATA_HOME=str(tmp_path / "data"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), XDG_RUNTIME_DIR=str(tmp_path / "runtime"),
               PATH=f"{venv / 'bin'}:/usr/bin:/bin")
    return env


def site_packages(venv):
    found = sorted(venv.glob("lib/python3*/site-packages"))
    assert found
    return found[0]


def test_the_installed_command_line_runs_and_says_it_is_a_pip_installation(installed, tmp_path):
    env = clean_env(tmp_path, installed)
    done = subprocess.run([str(installed / "bin/sandbox-ai"), "--version"], capture_output=True, text=True, env=env,
                          cwd=tmp_path, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == f"sandbox-ai {version()}", done.stderr
    verbose = subprocess.run([str(installed / "bin/sandbox-ai"), "version", "--verbose"], capture_output=True,
                             text=True, env=env, cwd=tmp_path, timeout=120)
    assert verbose.returncode == 0, verbose.stderr
    # A pip installation replaces itself with the release's wheel (tests/test_update_github.py does it against a
    # fake GitHub): it says so, hosts update takes that as its way, and its update asks the release source - here
    # nowhere - instead of naming a command.
    assert "updates: not through install.sh (a pip installation: the signed wheel of the release, sandbox-ai " \
           "update)" in verbose.stdout and "self-update: yes" in verbose.stdout
    hosts = load("hosts_pypi", ROOT / "sandbox/hosts.py")
    info = hosts.installation(verbose.stdout)
    assert info["kind"] == "pip" and info["version"] == version() and info["self_update"] is True
    assert Path(info["path"]).resolve() == site_packages(installed).resolve()
    assert hosts.not_updatable("box", info) is None
    assert "pip install -U sandbox-ai-app" in hosts.not_updatable("box", {**info, "self_update": False})
    nowhere = subprocess.run([str(installed / "bin/sandbox-ai"), "update"], capture_output=True, text=True,
                             env={**env, "SANDBOX_AI_SOURCE": "http://127.0.0.1:9/o/r"}, cwd=tmp_path, timeout=120)
    assert nowhere.returncode == 1 and "sandbox-ai update: cannot reach 127.0.0.1:9" in nowhere.stderr
    mode = subprocess.run([str(installed / "bin/sandbox-ai"), "update", "mode", "--json"], capture_output=True,
                          text=True, env=env, cwd=tmp_path, timeout=120)
    status = json.loads(mode.stdout)
    assert status["installed"] == version() and status["kind"] == "pip" and status["self_updating"] is True


def test_the_installed_command_line_reads_its_data_files(installed, tmp_path):
    """The commands that read the files added since 0.1.188 (the presets, Cairn's workers and their evidence, the
    fix plan's host facts, the toolchains, the MCP catalog) run from the installed wheel; offline, the catalogs say
    they cannot be read - never a traceback."""
    env = clean_env(tmp_path, installed)
    nowhere = "http://127.0.0.1:9"
    env.update(https_proxy=nowhere, HTTPS_PROXY=nowhere, http_proxy=nowhere, HTTP_PROXY=nowhere)
    env.pop("NO_PROXY", None), env.pop("no_proxy", None)

    def run(*args):
        done = subprocess.run([str(installed / "bin/sandbox-ai"), *args], capture_output=True, text=True, env=env,
                              cwd=tmp_path, timeout=300)
        assert "Traceback" not in done.stdout + done.stderr, (args, done.stderr[-3000:])
        return done

    presets = run("presets", "list")
    assert presets.returncode == 0, presets.stderr
    for name in ("android-development", "pair-reviewer", "docs-from-code"):
        assert name in presets.stdout, name
    shown = run("presets", "show", "android-development")
    assert shown.returncode == 0 and "android-development" in shown.stdout, shown.stderr
    workers = run("cairn", "workers")
    assert workers.returncode == 0 and "claude" in workers.stdout, workers.stderr
    doctor = run("doctor", "--json")
    checks = {c.get("check") for c in json.loads(doctor.stdout)}
    assert "sandbox-ai" in checks, sorted(checks)
    if sys.platform.startswith("linux"):  # hostinit.py: Podman's init binary (macOS: Podman's VM has its own)
        assert "Podman init" in checks, sorted(checks)
    assert run("mcp", "catalog").returncode in (0, 1)
    assert run("toolchain", "catalog").returncode in (0, 1)
    assert run("toolchain", "--help").returncode == 0


def test_the_installed_doctor_reports_the_pip_installation(installed, tmp_path):
    script = ("import importlib.util, json, sysconfig; from pathlib import Path\n"
              "here = Path(importlib.util.find_spec('sandbox_ai').submodule_search_locations[0])\n"
              "spec = importlib.util.spec_from_file_location('d', here / 'doctor.py')\n"
              "d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)\n"
              "facts = d.installation_facts()\n"
              "print(json.dumps([facts, d.where_text(facts), d.check_installation()[0]]))\n")
    done = subprocess.run([str(installed / "bin/python"), "-I", "-c", script], capture_output=True, text=True,
                          env=clean_env(tmp_path, installed), cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    import json
    facts, where, status = json.loads(done.stdout)
    assert facts["kind"] == "pip" and facts["version"] == version() and status == "ok"
    assert where.startswith("a pip installation (") and str(site_packages(installed).resolve()) in where


def test_the_installed_app_finds_the_command_line_of_its_own_installation(installed, tmp_path):
    """No SANDBOX_AI_HOME: the sandbox_ai beside the app, even when another sandbox-ai comes first on PATH."""
    other = tmp_path / "other"
    (other / "sandbox").mkdir(parents=True)
    for name in ("agent_sandbox.py", "main.py"):
        (other / "sandbox" / name).write_text("")
    (other / "sandbox-ai").write_text("#!/bin/sh\n")
    (other / "sandbox-ai").chmod(0o755)
    env = clean_env(tmp_path, installed)
    env["PATH"] = f"{other}:{env['PATH']}"
    script = ("import shutil; from sandbox_ai_app import backend\n"
              "print(shutil.which('sandbox-ai')); print(backend.locate(forbidden=[]))\n")
    done = subprocess.run([str(installed / "bin/python"), "-I", "-c", script], capture_output=True, text=True,
                          env=env, cwd=tmp_path, timeout=120)
    assert done.returncode == 0, done.stderr[-3000:]
    first, found = done.stdout.split()
    assert first == str(other / "sandbox-ai")
    assert Path(found) == site_packages(installed) / "sandbox_ai"
    # the app's own way to run it: sandbox-ai's main.py with the app's Python
    run = ("from sandbox_ai_app import backend; import subprocess\n"
           "s = backend.SandboxAI(); print(subprocess.run(s.command('--version'), capture_output=True, text=True)"
           ".stdout.strip())\n")
    done = subprocess.run([str(installed / "bin/python"), "-I", "-c", run], capture_output=True, text=True,
                          env=clean_env(tmp_path, installed), cwd=tmp_path, timeout=120)
    assert done.returncode == 0 and done.stdout.strip() == f"sandbox-ai {version()}", done.stderr[-3000:]


def test_the_app_without_pyside6_says_how_to_install_the_extra(installed, tmp_path):
    done = subprocess.run([str(installed / "bin/sandbox-ai-app"), "--self-test"], capture_output=True, text=True,
                          env=clean_env(tmp_path, installed), cwd=tmp_path, timeout=120)
    assert done.returncode == 1
    assert "Traceback" not in done.stderr, done.stderr
    assert "pip install 'sandbox-ai-app[gui]'" in done.stderr and "pipx install --force 'sandbox-ai-app[gui]'" \
        in done.stderr, done.stderr


def test_make_dist_checks_the_names_release_yml_uploads(tmp_path):
    make_dist = load("make_dist_names", ROOT / "scripts/make_dist.py")
    assert make_dist.python_package_files("1.2.3") == ("sandbox_ai_app-1.2.3-py3-none-any.whl",
                                                        "sandbox_ai_app-1.2.3.tar.gz")
    (tmp_path / "sandbox-ai-bundle-1.2.3.tar.gz").write_text("")
    (tmp_path / "sandbox-ai.pyz").write_text("")
    (tmp_path / "sandbox_ai-1.2.3-py3-none-any.whl").write_text("")
    (tmp_path / "sandbox_ai-1.2.3.tar.gz").write_text("")
    assert "sandbox_ai_app-1.2.3-py3-none-any.whl" in make_dist.python_package_problem(tmp_path, "1.2.3")
    for name in ("sandbox_ai-1.2.3-py3-none-any.whl", "sandbox_ai-1.2.3.tar.gz"):
        (tmp_path / name).unlink()
    for name in make_dist.python_package_files("1.2.3"):
        (tmp_path / name).write_text("")
    assert make_dist.python_package_problem(tmp_path, "1.2.3") is None
