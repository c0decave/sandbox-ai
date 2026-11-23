"""install.sh, scripts/make_dist.py and scripts/publish_release.py against a fake Gitea server."""
import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()  # tests write logins: never into the real HOME, also when conftest.py was not run

ROOT = REPOSITORY = Path(__file__).resolve().parents[1]
VERSION = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "sandbox/agent_sandbox.py").read_text(), re.M)[1]


def unsigned_copy(target):
    """The files make_dist.py would take from the repository (tracked and new, as --allow-dirty), in a Git
    repository of their own at TARGET, with sandbox/release-signers listing no key."""
    names = subprocess.run(["git", "-C", str(REPOSITORY), "ls-files", "-z", "--cached", "--others",
                            "--exclude-standard"], capture_output=True, text=True, check=True).stdout.split("\0")
    for name in filter(None, names):
        source = REPOSITORY / name
        if source.is_file() or source.is_symlink():
            (target / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / name, follow_symlinks=False)
    signers = target / "sandbox/release-signers"
    signers.write_text("".join(line for line in signers.read_text().splitlines(True) if line.startswith("#")))
    git = ["git", "-C", str(target), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
    subprocess.run(git + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-q", "-m", "copy"], check=True)
    return target


@pytest.fixture(scope="module", autouse=True)
def unsigned_tree(tmp_path_factory):
    """The tests release, install and update a tree that trusts only their own keys: the repository's
    sandbox/release-signers names the real release key, so a tree with it would refuse every test release."""
    global ROOT
    ROOT = unsigned_copy(tmp_path_factory.mktemp("tree"))
    yield ROOT
    ROOT = REPOSITORY


COMMANDS = ["sandbox-ai", "sandbox-ai-update"]
# The single commands of versions before 0.1.92: an update removes their links into the installation.
OLD_COMMANDS = ["claude-sandbox", "codex-sandbox", "opencode-sandbox", "sandbox-session", "sandbox-mcp",
                "sandbox-wizard", "sandbox-build-image"]
TOKEN = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


def make_dist(out, seconds=240):
    """scripts/make_dist.py into OUT, as a release builds it; one that does not finish within SECONDS (it hung on CI
    runners only) fails with where it waited: faulthandler's dump of the child, its frames on one line, which is what
    the CI annotation shows."""
    process = subprocess.Popen([sys.executable, "-X", "faulthandler", str(ROOT / "scripts/make_dist.py"),
                                "--allow-dirty", "--skip-python", "--out", str(out)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env={**os.environ, "SOURCE_DATE_EPOCH": "0"})
    try:
        _, err = process.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        process.send_signal(signal.SIGABRT)  # faulthandler writes every thread's frames to stderr
        _, err = process.communicate(timeout=30)
        frames = [line.strip() for line in err.decode(errors="replace").splitlines() if line.strip().startswith("File")]
        pytest.fail("make_dist hung: " + " < ".join(f.replace('File "', "").replace('", line ', ":") for f in frames)[:900])
    assert process.returncode == 0, err.decode(errors="replace")[-2000:]


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    make_dist(out)
    return out


class FakeGitea(http.server.ThreadingHTTPServer):
    """Releases API of one repository o/r, plus downloads and uploads; records Authorization headers.
    files is the release v<VERSION>; publish() adds others, the last one published is the newest."""
    def __init__(self, files):
        super().__init__(("127.0.0.1", 0), Handler)
        self.files, self.auth, self.uploads = dict(files), [], []
        self.tags, self.latest, self.rate_limited = {f"v{VERSION}": self.files}, f"v{VERSION}", False
        self.hooks = []  # what a notify channel's webhook (/hook) was sent
        self.made = None  # the release publish_release.py made: {"tag_name", "draft", "names"} (a draft at first)

    def publish(self, version, files):
        self.tags[f"v{version}"] = files
        self.latest = f"v{version}"

    def release(self, tag):
        base = f"http://127.0.0.1:{self.server_address[1]}"
        return {"id": 1, "tag_name": tag, "draft": False, "prerelease": False,
                "assets": [{"id": i, "name": name, "browser_download_url": f"{base}/o/r/releases/download/{tag}/{name}"}
                           for i, name in enumerate(sorted(self.tags[tag]), 1)]}


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, body=b"", kind="application/json"):
        body = json.dumps(body).encode() if isinstance(body, (dict, list)) else body
        self.send_response(code)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        if self.server.rate_limited and self.path.startswith("/api/"):
            body = json.dumps({"message": "API rate limit exceeded"}).encode()
            self.send_response(403)
            for name, value in (("X-RateLimit-Remaining", "0"), ("X-RateLimit-Reset", str(int(time.time()) + 600)),
                                ("Content-Length", str(len(body)))):
                self.send_header(name, value)
            self.end_headers()
            return self.wfile.write(body)
        if self.path == "/api/v1/repos/o/r/releases/latest":
            return self.reply(200, self.server.release(self.server.latest))
        match = re.fullmatch(r"/api/v1/repos/o/r/releases/tags/(.+)", self.path)
        if match and match[1] in self.server.tags:
            return self.reply(200, self.server.release(match[1]))
        made = self.server.made
        if self.path == "/api/v1/repos/o/r/releases/1" and made:  # (the release publish_release.py made)
            return self.reply(200, {"id": 1, "tag_name": made["tag_name"], "draft": made["draft"],
                                    "assets": [{"id": i, "name": name, "size": 1}
                                               for i, name in enumerate(made["names"], 1)]})
        match = re.fullmatch(r"/o/r/releases/download/([^/]+)/(.+)", self.path)
        if match and match[2] in self.server.tags.get(match[1], {}):
            return self.reply(200, self.server.tags[match[1]][match[2]], "application/octet-stream")
        self.reply(404, {"message": "not found"})

    def do_POST(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/hook":
            self.server.hooks.append(json.loads(body))
            return self.reply(200, {})
        if self.path == "/api/v1/repos/o/r/releases":
            asked = json.loads(body)
            self.server.made = {"tag_name": asked["tag_name"], "draft": bool(asked.get("draft")), "names": []}
            return self.reply(201, {"id": 1, "tag_name": asked["tag_name"], "draft": self.server.made["draft"],
                                    "assets": []})
        if self.path.startswith("/api/v1/repos/o/r/releases/1/assets"):
            self.server.uploads.append((self.path, self.headers.get("Content-Type"), body))
            if self.server.made:
                self.server.made["names"].append(re.search(r"name=([^&]+)", self.path)[1])
            return self.reply(201, {"id": len(self.server.uploads)})
        self.reply(404)

    def do_PATCH(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        if self.path == "/api/v1/repos/o/r/releases/1" and self.server.made:
            self.server.made.update(body)
            return self.reply(200, {"id": 1, "tag_name": self.server.made["tag_name"],
                                    "draft": self.server.made["draft"]})
        self.reply(404)

    def do_DELETE(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        self.server.uploads.append((self.path, "DELETE", b""))
        self.reply(204)


@pytest.fixture(autouse=True)
def nothing_left(tmp_path):
    """Each test's folder goes when it ends: its installations and bundles (some 150 MB a test, up to 1 GB; pytest
    keeps every test's folder until the session ends, and the folders of three sessions - a full run filled the
    disk of a machine near full)."""
    yield
    home_guard.remove(tmp_path)


@pytest.fixture
def gitea(dist):
    server = FakeGitea({p.name: p.read_bytes() for p in dist.iterdir()})
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def environment(tmp_path, **extra):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("XDG_", "SANDBOX_AI_"))}
    return {**env, "HOME": str(home), "PATH": "/usr/bin:/bin", **extra}


def install(tmp_path, *args, **extra):
    # PySide6 for the app is never downloaded here; test_app_* cover that step with a stand-in pip.
    app = [] if any(a.startswith(("--app-", "--no-app")) for a in args) else ["--no-app-deps", "--no-app-terminal"]
    return subprocess.run(["bash", str(ROOT / "install.sh"), "--user", *args, *app], capture_output=True, text=True,
                          env=environment(tmp_path, **extra), timeout=120)


def installed(tmp_path):
    return tmp_path / "home/.local/share/sandbox-ai"


def test_install_from_checkout(scenario, tmp_path):
    if scenario == "edge":
        (tmp_path / "home").mkdir()
        (tmp_path / "home/.bin").mkdir()  # an existing ~/.bin gets the links too
    result = install(tmp_path, "--from", str(ROOT))
    assert result.returncode == 0, result.stderr
    data, bins = installed(tmp_path), [tmp_path / "home/.local/bin"]
    if scenario == "edge":
        bins.append(tmp_path / "home/.bin")
    for directory in bins:
        for command in COMMANDS:
            link = directory / command
            assert link.is_symlink() and str(os.readlink(link)).startswith(str(data)), link
    assert (data / "current").resolve() == data / "versions" / VERSION
    version = subprocess.run([str(bins[0] / "sandbox-ai"), "--version"], capture_output=True, text=True)
    assert not any((directory / command).exists() or (directory / command).is_symlink()
                   for directory in bins for command in OLD_COMMANDS)
    assert version.stdout.strip() == f"sandbox-ai {VERSION}"
    # No local tool state or VCS data travels along.
    assert not (data / "current/.git").exists() and not (data / "current/.claude").exists()
    # The large modules are compiled once at installation (the version folder is read-only to its users later:
    # without it every command compiled them again).
    tag = subprocess.run(["python3", "-I", "-c", "import sys; print(sys.implementation.cache_tag)"],
                         capture_output=True, text=True, env=environment(tmp_path)).stdout.strip()  # the installer's
    assert (data / "current/sandbox/__pycache__" / f"agent_sandbox.{tag}.pyc").is_file()
    assert (data / "current/sandbox/__pycache__" / f"runtime.{tag}.pyc").is_file()
    # The installed copy updates itself through its installer.
    check = subprocess.run([str(bins[0] / "sandbox-ai"), "update", "--user", "--check", "--from", str(ROOT)],
                           capture_output=True, text=True, env=environment(tmp_path))
    assert check.returncode == 0 and f"installed: {VERSION}" in check.stdout, check.stderr
    if scenario == "sad":
        # Uninstall removes links and data, nothing else.
        (tmp_path / "home/.local/bin/other-tool").write_text("keep")
        result = install(tmp_path, "--uninstall")
        assert result.returncode == 0, result.stderr
        assert not data.exists() and not any((tmp_path / "home/.local/bin" / c).exists() for c in COMMANDS)
        assert (tmp_path / "home/.local/bin/other-tool").read_text() == "keep"


APP = ROOT / "projects/sandbox-ai-app"
needs_app = pytest.mark.skipif(not (APP / "sandbox_ai_app/__main__.py").is_file(),
                               reason="the checkout has no projects/sandbox-ai-app")


@needs_app
def test_app_installed_with_command_and_menu_entry(tmp_path):
    result = install(tmp_path, "--from", str(ROOT), "--no-app-deps", "--no-app-terminal")
    assert result.returncode == 0, result.stderr
    data, home = installed(tmp_path), tmp_path / "home"
    link = home / ".local/bin/sandbox-ai-app"
    assert link.is_symlink() and os.readlink(link) == str(data / "current/sandbox-ai-app")
    app = data / "current/projects/sandbox-ai-app"
    assert (app / "sandbox_ai_app/__main__.py").is_file() and (app / "scripts/vendor_xterm.py").is_file()
    # Only the app repository's own files: no caches, no VCS data, no build output.
    # Nothing of the checkout's own travels along: bytecode only as the installer made it (its python3's).
    tag = subprocess.run(["python3", "-I", "-c", "import sys; print(sys.implementation.cache_tag)"],
                         capture_output=True, text=True, env=environment(tmp_path)).stdout.strip()
    assert all(f".{tag}." in pyc.name for pyc in app.rglob("*.pyc")), "bytecode copied from the checkout"
    assert not (app / ".git").exists() and not list(app.glob("*.egg-info"))
    entry = (home / ".local/share/applications/sandbox-ai-app.desktop").read_text()
    assert f"Exec={home}/.local/bin/sandbox-ai-app\n" in entry and "Terminal=false" in entry
    assert not (data / "app-env").exists()  # --no-app-deps
    # Without PySide6 the command says what to do instead of failing with a traceback.
    started = subprocess.run([str(link)], capture_output=True, text=True, env=environment(tmp_path))
    if started.returncode != 0 and "PySide6" in started.stderr:
        assert "Traceback" not in started.stderr
    removed = install(tmp_path, "--uninstall")
    assert removed.returncode == 0, removed.stderr
    assert not link.exists() and not link.is_symlink()
    assert not (home / ".local/share/applications/sandbox-ai-app.desktop").exists()


@needs_app
def test_no_app_links_nothing_for_it(tmp_path):
    result = install(tmp_path, "--from", str(ROOT), "--no-app")
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "home/.local/bin/sandbox-ai-app").exists()
    assert not (tmp_path / "home/.local/share/applications/sandbox-ai-app.desktop").exists()
    assert (tmp_path / "home/.local/bin/sandbox-ai").is_symlink()


@needs_app
def test_app_dependencies_go_into_their_own_environment(tmp_path):
    """--app-deps: a venv below the installation, pip inside it (a stand-in here: nothing is downloaded)."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    log = tmp_path / "pip.log"
    # python3 that creates a "venv" whose python3 records pip calls; everything else goes to the real one.
    (fake / "python3").write_text(f"""#!/bin/bash
if [[ "$*" == "-I -m venv "* ]]; then
  env="${{@: -1}}"; mkdir -p "$env/bin"
  printf '#!/bin/bash\\nif [[ "$*" == *" pip install "* ]]; then echo "$*" >> {log}; exit 0; fi\\nexit 1\\n' > "$env/bin/python3"
  chmod +x "$env/bin/python3"; exit 0
fi
exec {sys.executable} "$@"
""")
    (fake / "python3").chmod(0o755)
    result = install(tmp_path, "--from", str(ROOT), "--app-deps", "--no-app-terminal", PATH=f"{fake}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    env = installed(tmp_path) / "app-env/bin/python3"
    assert env.is_file()
    assert "pip install" in log.read_text() and "PySide6-Essentials>=6.6,!=6.12.0; python_version < '3.12'" in log.read_text()
    assert "WebEngine" not in log.read_text()
    assert "Qt does not load" in result.stderr  # the stand-in cannot import PySide6.QtWidgets
    # By default terminal tabs come along: QtWebEngine, and xterm.js through the app's vendoring script.
    log.write_text("")
    vendored = tmp_path / "vendor.log"
    (fake / "python3").write_text((fake / "python3").read_text().replace(
        "exec ", f'[[ "$*" == *vendor_xterm.py* ]] && {{ echo "$*" >> {vendored}; exit 0; }}\nexec ', 1))
    again = install(tmp_path, "--from", str(ROOT), "--app-deps", "--force", PATH=f"{fake}:/usr/bin:/bin")
    assert again.returncode == 0, again.stderr
    assert "PySide6-WebEngine>=6.12; python_version >= '3.12'" in log.read_text()
    assert "vendor_xterm.py" in vendored.read_text()


def test_install_latest_release_with_checksum(scenario, tmp_path, gitea):
    source = f"http://127.0.0.1:{gitea.server_address[1]}/o/r"
    bundle = f"sandbox-ai-bundle-{VERSION}.tar.gz"
    if scenario == "sad":
        # A tampered bundle is refused before anything is installed.
        gitea.files[bundle] = gitea.files[bundle][:-10] + b"tampered!!"
        result = install(tmp_path, "--source", source)
        assert result.returncode != 0 and "checksum mismatch" in result.stderr
        assert not (installed(tmp_path) / "current").exists()
        return
    token = {"SANDBOX_AI_TOKEN": TOKEN} if scenario == "edge" else {}
    result = install(tmp_path, "--source", source, **token)
    assert result.returncode == 0, result.stderr
    assert "SHA-256 verified" in result.stderr and (installed(tmp_path) / "versions" / VERSION).is_dir()
    assert TOKEN not in result.stdout + result.stderr
    sent = {auth for _, auth in gitea.auth}
    assert sent == ({f"token {TOKEN}"} if scenario == "edge" else {None})
    # Running it again changes nothing: already the newest.
    again = install(tmp_path, "--source", source, **token)
    assert again.returncode == 0 and "up to date" in again.stderr
    check = install(tmp_path, "--source", source, "--check")
    assert f"installed: {VERSION}" in check.stdout and f"available: {VERSION}" in check.stdout


def test_an_update_removes_the_links_of_the_old_single_commands(tmp_path):
    """claude-sandbox, sandbox-session, ... were removed: an update (or a new install) takes away their links into
    the installation, made by an earlier version, and says so; a file of that name that is not ours stays, and
    --uninstall takes the links away too."""
    assert install(tmp_path, "--from", str(ROOT)).returncode == 0
    data, bin_dir = installed(tmp_path), tmp_path / "home/.local/bin"
    for command in OLD_COMMANDS[:-1]:
        (bin_dir / command).symlink_to(data / "current" / command)  # as an earlier version linked them
    (bin_dir / OLD_COMMANDS[-1]).write_text("#!/bin/sh\necho mine\n")
    result = install(tmp_path, "--from", str(ROOT), "--force")
    assert result.returncode == 0, result.stderr
    assert not any((bin_dir / command).is_symlink() for command in OLD_COMMANDS)
    assert f"removed {bin_dir / 'claude-sandbox'}: use sandbox-ai instead" in result.stderr
    assert (bin_dir / OLD_COMMANDS[-1]).read_text() == "#!/bin/sh\necho mine\n"
    (bin_dir / "sandbox-wizard").symlink_to(data / "current" / "sandbox-wizard")
    assert install(tmp_path, "--uninstall").returncode == 0
    assert not (bin_dir / "sandbox-wizard").is_symlink() and (bin_dir / OLD_COMMANDS[-1]).exists()


def test_install_protects_foreign_files_and_refuses_bad_input(scenario, tmp_path, gitea):
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    if scenario == "happy":
        (bin_dir / "sandbox-ai").write_text("#!/bin/sh\necho mine\n")
        result = install(tmp_path, "--from", str(ROOT))
        assert result.returncode == 0 and "does not belong to sandbox-ai" in result.stderr
        assert (bin_dir / "sandbox-ai").read_text() == "#!/bin/sh\necho mine\n"
        forced = install(tmp_path, "--from", str(ROOT), "--force")
        assert forced.returncode == 0 and (bin_dir / "sandbox-ai").is_symlink()
    elif scenario == "sad":
        # The token never goes over plain http to anything but localhost.
        result = install(tmp_path, "--source", "http://git.example.invalid/o/r", SANDBOX_AI_TOKEN=TOKEN)
        assert result.returncode != 0 and "refusing to send SANDBOX_AI_TOKEN over plain http" in result.stderr
        assert TOKEN not in result.stderr
        assert "--version expects" in install(tmp_path, "--version", "1.0; rm -rf /").stderr
    else:
        # A bundle with an escaping entry is refused.
        evil = tmp_path / "evil.tar.gz"
        with tarfile.open(evil, "w:gz") as tar:
            payload = tmp_path / "x"
            payload.write_text("x")
            tar.add(payload, arcname="sandbox-ai-9/../../escape")
        result = install(tmp_path, "--from", str(evil))
        assert result.returncode != 0 and "unsafe entry" in result.stderr
        assert not (tmp_path / "escape").exists()


def bundle_difference(first, second):
    """What differs between two bundles: members only in one, members whose content, mode or time differ."""
    def members(path):
        found = {}
        with tarfile.open(path) as tar:
            for member in tar.getmembers():
                data = tar.extractfile(member).read() if member.isfile() else b""
                found[member.name] = (hashlib.sha256(data).hexdigest()[:12], member.mode, member.mtime, member.uid)
        return found
    a, b = members(first), members(second)
    parts = [f"only in the first: {n}" for n in sorted(set(a) - set(b))][:5]
    parts += [f"only in the second: {n}" for n in sorted(set(b) - set(a))][:5]
    parts += [f"differs: {n} {a[n]} {b[n]}" for n in sorted(set(a) & set(b)) if a[n] != b[n]][:5]
    return "; ".join(parts) or "the same members (the compression differs)"


def test_make_dist_is_reproducible(scenario, tmp_path, dist):
    other = tmp_path / "again"
    make_dist(other)
    name = f"sandbox-ai-bundle-{VERSION}.tar.gz"
    if (other / name).read_bytes() != (dist / name).read_bytes():
        # Said by member, never as a diff of two archives (pytest's diff of megabytes ran for an hour on CI).
        pytest.fail("the bundle is not reproducible: " + bundle_difference(dist / name, other / name))
    sums = dict(reversed(line.split("  ")) for line in (dist / "SHA256SUMS").read_text().splitlines())
    assert sums[name] == hashlib.sha256((dist / name).read_bytes()).hexdigest()
    with tarfile.open(dist / name) as tar:
        names = tar.getnames()
        if scenario == "happy":
            assert f"sandbox-ai-{VERSION}/install.sh" in names and f"sandbox-ai-{VERSION}/sandbox/agent_sandbox.py" in names
        elif scenario == "sad":
            assert not any("/.claude/" in n or "/.git/" in n or "__pycache__" in n for n in names)
        else:
            assert all(m.uid == 0 and m.gid == 0 and m.mtime == 0 for m in tar.getmembers())


def test_publish_release(scenario, tmp_path, gitea, dist):
    source = f"http://127.0.0.1:{gitea.server_address[1]}/o/r"
    files = [str(dist / "SHA256SUMS"), str(dist / "install.sh")]
    env = {**os.environ, "RELEASE_TOKEN": TOKEN}
    # (two files of a release, to a Gitea of one's own: --partial; tests/test_release_assets.py has the whole set)
    args = [sys.executable, str(ROOT / "scripts/publish_release.py"), "--source", source, "--token-env",
            "RELEASE_TOKEN", "--partial"]
    if scenario == "sad":
        result = subprocess.run([*args, *files], capture_output=True, text=True, env={**env, "RELEASE_TOKEN": ""})
        assert result.returncode != 0 and "RELEASE_TOKEN" in result.stderr
        result = subprocess.run([*args[:3], "http://git.example.invalid/o/r", "--token-env", "RELEASE_TOKEN",
                                 "--partial", *files], capture_output=True, text=True, env=env)
        assert result.returncode != 0 and "plain http" in result.stderr
        return
    tag = ["--tag", "v9.9.9"] if scenario == "edge" else []
    result = subprocess.run([*args, *tag, *files], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert all(auth == f"token {TOKEN}" for _, auth in gitea.auth)
    uploads = [u for u in gitea.uploads if u[1] != "DELETE"]
    assert [re.search(r"name=([^&]+)", u[0])[1] for u in uploads] == ["SHA256SUMS", "install.sh"]
    assert all(u[1].startswith("multipart/form-data") and b'name="attachment"' in u[2] for u in uploads)
    if scenario == "happy":
        # The release v<version> already had these files: they are replaced, not duplicated.
        assert [u for u in gitea.uploads if u[1] == "DELETE"]
        assert "created release" not in result.stderr
    else:
        # A new release is a draft until its last file is up: releases/latest never names a half-uploaded one.
        assert "created release v9.9.9 as a draft" in result.stderr and "published release v9.9.9" in result.stderr
        assert gitea.made == {"tag_name": "v9.9.9", "draft": False, "names": ["SHA256SUMS", "install.sh"]}


# --- signed releases --------------------------------------------------------------------------------------

needs_ssh = pytest.mark.skipif(not shutil.which("ssh-keygen"), reason="ssh-keygen (OpenSSH) is needed")
NAMESPACE = "sandbox-ai-release"


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rk = load(ROOT / "sandbox/release_keys.py", "release_keys_for_tests")


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    """Throwaway ed25519 keys: old and new (the project's, before and after a rotation) and evil (anybody's)."""
    directory = tmp_path_factory.mktemp("keys")
    made = {}
    for name in ("old", "new", "evil"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(directory / name)],
                       check=True)
        kind, key = (directory / f"{name}.pub").read_text().split()[:2]
        made[name] = SimpleNamespace(path=directory / name, kind=kind, key=key, line=rk.line(kind, key),
                                     fingerprint=rk.fingerprint(key))
    return made


def ssh_sign(data, key, namespace=NAMESPACE):
    return subprocess.run(["ssh-keygen", "-Y", "sign", "-f", str(key.path), "-n", namespace], input=data,
                          capture_output=True, check=True).stdout


def build_release(dist, version, trusted=(), signer=None, namespace=NAMESPACE):
    """The files of release VERSION: the dist bundle as VERSION with the release keys TRUSTED in its tree,
    SHA256SUMS and, signed by SIGNER, SHA256SUMS.sig."""
    raw = io.BytesIO()
    signers = "# test keys\n" + "".join(f"{key.line} {key.path.name}\n" for key in trusted)
    with tarfile.open(dist / f"sandbox-ai-bundle-{VERSION}.tar.gz") as source, \
            tarfile.open(fileobj=raw, mode="w:gz", compresslevel=1) as target:
        for member in source.getmembers():
            data = source.extractfile(member).read() if member.isfile() else None
            relative = member.name.partition("/")[2]
            if relative == "sandbox/agent_sandbox.py":
                data = data.replace(f'__version__ = "{VERSION}"'.encode(), f'__version__ = "{version}"'.encode())
            elif relative == "sandbox/release-signers":
                data = signers.encode()
            member.name = member.name.replace(f"sandbox-ai-{VERSION}", f"sandbox-ai-{version}", 1)
            # A path over 100 characters (the vendored skills have some) lives in the PAX header, which tarfile
            # writes back as it was: renamed there too, or the bundle gets two top directories.
            if "path" in member.pax_headers:
                member.pax_headers["path"] = member.name
            if data is not None:
                member.size = len(data)
            target.addfile(member, io.BytesIO(data) if data is not None else None)
    name = f"sandbox-ai-bundle-{version}.tar.gz"
    files = {name: raw.getvalue()}
    files["SHA256SUMS"] = f"{hashlib.sha256(files[name]).hexdigest()}  {name}\n".encode()
    if signer:
        files["SHA256SUMS.sig"] = ssh_sign(files["SHA256SUMS"], signer, namespace)
    return files


def update(tmp_path, *args, **extra):
    """sandbox-ai update of the installed tree (its own install.sh)."""
    return subprocess.run([str(tmp_path / "home/.local/bin/sandbox-ai"), "update", "--user", "--no-app-deps",
                           "--no-app-terminal", *args], capture_output=True, text=True,
                          env=environment(tmp_path, **extra), timeout=120)


def current(tmp_path):
    link = installed(tmp_path) / "current"
    return link.resolve().name if link.exists() else None


def trusting(tmp_path, gitea, dist, *trusted, version="9.1.0"):
    """An installation of release VERSION whose tree trusts these keys (the first install: trust on first use)."""
    gitea.publish(version, build_release(dist, version, trusted, trusted[0] if trusted else None))
    result = install(tmp_path, "--source", source_of(gitea))
    assert result.returncode == 0 and current(tmp_path) == version, result.stderr
    return result


def source_of(gitea):
    return f"http://127.0.0.1:{gitea.server_address[1]}/o/r"


@needs_ssh
def test_signed_update_installs_and_shows_its_key(scenario, tmp_path, gitea, dist, keys):
    first = trusting(tmp_path, gitea, dist, keys["old"])
    assert f"from now on, updates must be signed by: {keys['old'].fingerprint}" in first.stderr
    gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["old"]))
    if scenario == "happy":
        # sandbox-ai update through the installed tree's own installer and keys.
        result = update(tmp_path, "--source", source_of(gitea))
        assert result.returncode == 0, result.stderr
        assert f"SHA-256 verified, signed by {keys['old'].fingerprint}" in result.stderr
        assert current(tmp_path) == "9.2.0"
        assert json.loads((installed(tmp_path) / "release-state.json").read_text())["highest_seen"] == "9.2.0"
    elif scenario == "sad":
        check = update(tmp_path, "--source", source_of(gitea), "--check")
        assert check.returncode == 0, check.stderr
        assert "installed: 9.1.0" in check.stdout and "available: 9.2.0" in check.stdout
        assert f"signature: good, by the trusted key {keys['old'].fingerprint}" in check.stdout
        assert f"trusted keys: {keys['old'].fingerprint} (the installed tree)" in check.stdout
        assert current(tmp_path) == "9.1.0"  # --check changes nothing
    else:
        verbose = subprocess.run([str(tmp_path / "home/.local/bin/sandbox-ai"), "version", "--verbose"],
                                 capture_output=True, text=True, env=environment(tmp_path))
        assert verbose.returncode == 0, verbose.stderr
        assert "sandbox-ai 9.1.0" in verbose.stdout and f"  {keys['old'].fingerprint} (ssh-ed25519)" in verbose.stdout
        assert "must be signed by one of these keys" in verbose.stdout
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["new"]))
        check = update(tmp_path, "--source", source_of(gitea), "--check")
        assert f"signature: BAD: signed by {keys['new'].fingerprint}, which is not a trusted release key" in check.stdout
        assert "an update refuses this release without --allow-unsigned" in check.stdout


@needs_ssh
def test_bad_signatures_are_refused(scenario, tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"])
    source = source_of(gitea)
    release = build_release(dist, "9.2.0", [keys["old"]], keys["old"])
    if scenario == "happy":
        del release["SHA256SUMS.sig"]  # no signature: refused while the installed tree trusts a key
        expected = "the release 9.2.0 is not signed (the release has no SHA256SUMS.sig)"
    elif scenario == "sad":
        # SHA256SUMS changed after signing (say, to match a replaced bundle): the signature no longer fits.
        release["SHA256SUMS"] += b"0" * 64 + b"  other-file\n"
        expected = "the signature does not match SHA256SUMS"
    else:
        release["SHA256SUMS.sig"] = ssh_sign(release["SHA256SUMS"], keys["evil"])
        expected = f"signed by {keys['evil'].fingerprint}, which is not a trusted release key"
    gitea.publish("9.2.0", release)
    result = update(tmp_path, "--source", source)
    assert result.returncode != 0 and expected in result.stderr, result.stderr
    assert "nothing installed" in result.stderr and current(tmp_path) == "9.1.0"
    assert f"Trusted release keys: {keys['old'].fingerprint}" in result.stderr
    if scenario == "edge":
        # The project's own key, but a signature made for another purpose (namespace): refused too.
        release["SHA256SUMS.sig"] = ssh_sign(release["SHA256SUMS"], keys["old"], namespace="file")
        result = update(tmp_path, "--source", source)
        assert result.returncode != 0 and "the signature is for 'file', not 'sandbox-ai-release'" in result.stderr
        assert current(tmp_path) == "9.1.0"


@needs_ssh
def test_allow_unsigned_warns_but_never_changes_the_keys(scenario, tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"])
    if scenario == "happy":
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]]))
        result = update(tmp_path, "--source", source_of(gitea), "--allow-unsigned")
        assert result.returncode == 0, result.stderr
        assert "WARNING: the release 9.2.0 is not signed" in result.stderr and "--allow-unsigned" in result.stderr
        assert current(tmp_path) == "9.2.0"
    else:
        # A release no trusted key signed cannot bring keys of its own (it would own every later update).
        signer = keys["evil"] if scenario == "edge" else None
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["evil"]], signer))
        refused = update(tmp_path, "--source", source_of(gitea))
        assert refused.returncode != 0 and current(tmp_path) == "9.1.0"
        result = update(tmp_path, "--source", source_of(gitea), "--allow-unsigned")
        assert result.returncode != 0, result.stderr
        assert "would change the trusted release keys" in result.stderr and current(tmp_path) == "9.1.0"
        assert (installed(tmp_path) / "current/sandbox/release-signers").read_text().count(keys["old"].key) == 1


@needs_ssh
def test_key_rotation_needs_a_signature_by_a_trusted_key(tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"])
    source = source_of(gitea)
    # 1. old signs a release that adds new.
    gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"], keys["new"]], keys["old"]))
    result = update(tmp_path, "--source", source)
    assert result.returncode == 0, result.stderr
    assert f"the release keys change with this version (signed by a trusted key): added {keys['new'].fingerprint}" \
        in result.stderr
    # 2. new signs a release that drops old.
    gitea.publish("9.3.0", build_release(dist, "9.3.0", [keys["new"]], keys["new"]))
    result = update(tmp_path, "--source", source)
    assert result.returncode == 0 and f"removed {keys['old'].fingerprint}" in result.stderr, result.stderr
    # 3. old no longer counts.
    gitea.publish("9.4.0", build_release(dist, "9.4.0", [keys["new"]], keys["old"]))
    result = update(tmp_path, "--source", source)
    assert result.returncode != 0 and "not a trusted release key" in result.stderr
    assert current(tmp_path) == "9.3.0"


@needs_ssh
def test_downgrades_are_refused(scenario, tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"], version="9.2.0")
    source = source_of(gitea)
    gitea.publish("9.1.0", build_release(dist, "9.1.0", [keys["old"]], keys["old"]))  # an old release, served as newest
    if scenario == "happy":
        result = update(tmp_path, "--source", source)
        assert result.returncode != 0 and current(tmp_path) == "9.2.0"
        assert "sandbox-ai 9.1.0 is older than the installed 9.2.0, although the source lists it as the newest" \
            in result.stderr
    elif scenario == "sad":
        result = update(tmp_path, "--source", source, "--version", "9.1.0")
        assert result.returncode != 0 and "--allow-downgrade installs it anyway" in result.stderr
        assert current(tmp_path) == "9.2.0"
    else:
        result = update(tmp_path, "--source", source, "--version", "9.1.0", "--allow-downgrade")
        assert result.returncode == 0 and "downgrading from 9.2.0 to 9.1.0" in result.stderr, result.stderr
        assert current(tmp_path) == "9.1.0"
        # The newest seen is remembered: an older "newest" release is pointed out, though still an upgrade here.
        gitea.publish("9.1.5", build_release(dist, "9.1.5", [keys["old"]], keys["old"]))
        result = update(tmp_path, "--source", source)
        assert result.returncode == 0 and current(tmp_path) == "9.1.5", result.stderr
        assert "this installation has seen 9.2.0 before" in result.stderr


def policy_installer(tmp_path, document):
    """install.sh reading a policy file of this test (owned by the test's user instead of root)."""
    directory = tmp_path / "etc"
    directory.mkdir(mode=0o755)
    (directory / "policy.json").write_text(json.dumps(document))
    text = (ROOT / "install.sh").read_text()
    assert "POLICY_FILE=/etc/sandbox-ai/policy.json\nPOLICY_OWNER=0\n" in text
    text = text.replace("POLICY_FILE=/etc/sandbox-ai/policy.json\nPOLICY_OWNER=0\n",
                        f"POLICY_FILE={directory}/policy.json\nPOLICY_OWNER={os.getuid()}\n")
    (tmp_path / "policy-install.sh").write_text(text)
    return directory / "policy.json"


@needs_ssh
def test_policy_requires_signed_updates(scenario, tmp_path, gitea, dist, keys):
    policy = policy_installer(tmp_path, {"version": 1, "require_signed_updates": True})

    def run(*args):
        return subprocess.run(["bash", str(tmp_path / "policy-install.sh"), "--user", "--no-app-deps",
                               "--no-app-terminal", "--source", source_of(gitea), *args],
                              capture_output=True, text=True, env=environment(tmp_path), timeout=120)
    if scenario == "happy":
        trusting(tmp_path, gitea, dist, keys["old"])
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]]))
        result = run("--allow-unsigned")
        assert result.returncode != 0 and "--allow-unsigned is not allowed by the administrator's policy" \
            in result.stderr
        assert current(tmp_path) == "9.1.0"
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["old"]))
        result = run()
        assert result.returncode == 0 and current(tmp_path) == "9.2.0", result.stderr
        assert "policy: signed updates required" in run("--check").stdout
    elif scenario == "sad":
        # No key trusted anywhere: nothing can be checked, so nothing is installed.
        gitea.publish("9.1.0", build_release(dist, "9.1.0", [keys["old"]], keys["old"]))
        result = run()
        assert result.returncode != 0 and "requires signed updates" in result.stderr and current(tmp_path) is None
        result = run("--from", str(ROOT))
        assert result.returncode != 0 and "requires signed updates" in result.stderr and current(tmp_path) is None
    else:
        policy.chmod(0o666)  # a file anybody could change is no policy: fail closed
        result = run()
        assert result.returncode != 0 and "must be owned by root and writable by root only" in result.stderr
        policy.chmod(0o644)
        policy.write_text(json.dumps({"version": 1, "require_signed_updates": "yes"}))
        result = run()
        assert result.returncode != 0 and "require_signed_updates is true or false" in result.stderr


@needs_ssh
def test_first_install_trusts_the_keys_embedded_in_install_sh(scenario, tmp_path, gitea, dist, keys):
    """curl .../install.sh | bash: no tree yet, so the keys make_dist.py wrote into install.sh check the release."""
    make_dist = load(ROOT / "scripts/make_dist.py", "make_dist_for_tests")
    text = make_dist.embed_signers((ROOT / "install.sh").read_text(), [(keys["old"].kind, keys["old"].key)])
    assert f"EMBEDDED_SIGNERS='{keys['old'].line}'\n" in text
    signer = {"happy": keys["old"], "sad": keys["evil"], "edge": None}[scenario]
    gitea.publish("9.1.0", build_release(dist, "9.1.0", [keys["old"]], signer))
    result = subprocess.run(["bash", "-s", "--", "--user", "--no-app-deps", "--no-app-terminal", "--source",
                             source_of(gitea)], input=text, capture_output=True, text=True,
                            env=environment(tmp_path), timeout=120)
    if scenario == "happy":
        assert result.returncode == 0, result.stderr
        assert f"signed by {keys['old'].fingerprint}" in result.stderr and current(tmp_path) == "9.1.0"
        # From now on the installed tree's keys count.
        assert keys["old"].key in (installed(tmp_path) / "current/sandbox/release-signers").read_text()
    else:
        assert result.returncode != 0 and current(tmp_path) is None
        assert ("not a trusted release key" if scenario == "sad" else "is not signed") in result.stderr
        assert "the keys embedded in install.sh (trust on first use)" in result.stderr


@needs_ssh
def test_from_bundle_with_verify(scenario, tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"])
    local = tmp_path / "download"
    local.mkdir()
    release = build_release(dist, "9.2.0", [keys["old"]], keys["old"])
    if scenario == "sad":
        release["SHA256SUMS.sig"] = ssh_sign(release["SHA256SUMS"], keys["evil"])
    for name, data in release.items():
        (local / name).write_bytes(data)
    bundle = local / "sandbox-ai-bundle-9.2.0.tar.gz"
    if scenario == "edge":
        bundle.write_bytes(bundle.read_bytes()[:-10] + b"tampered!!")
    result = install(tmp_path, "--from", str(bundle), "--verify")
    if scenario == "happy":
        assert result.returncode == 0, result.stderr
        assert f"against SHA256SUMS and its signature by {keys['old'].fingerprint}" in result.stderr
        assert current(tmp_path) == "9.2.0"
        directory = install(tmp_path, "--from", str(ROOT), "--verify")
        assert directory.returncode != 0 and "not a directory" in directory.stderr
    else:
        assert result.returncode != 0 and current(tmp_path) == "9.1.0"
        assert ("not a trusted release key" if scenario == "sad" else "checksum mismatch") in result.stderr


def test_github_rate_limit_is_explained(tmp_path, gitea):
    gitea.rate_limited = True
    result = install(tmp_path, "--source", source_of(gitea))
    assert result.returncode != 0 and "limits the requests to its API (HTTP 403); try again after" in result.stderr
    assert "SANDBOX_AI_TOKEN" in result.stderr and current(tmp_path) is None


def test_install_sh_checks_keys_like_release_keys():
    """install.sh carries its own copy of the key rules (it runs before any tree exists): the same ones."""
    import ast
    text = (ROOT / "install.sh").read_text()
    copied = re.search(r"\nKEY_LINE = re\.compile\((.*?)\)\n", text, re.S)[1]
    assert ast.literal_eval(f"({copied})") == rk.KEY_LINE.pattern
    assert f'NAMESPACE, PRINCIPAL = "{rk.NAMESPACE}", "{rk.PRINCIPAL}"' in text
    # The repository lists the release key (no test key, every line in the one allowed form); install.sh in
    # the repository embeds none: make_dist.py writes them into the published copy.
    listed = rk.load(REPOSITORY / "sandbox/release-signers")
    assert listed and all(line.startswith(f"{rk.PRINCIPAL} ") for line in (REPOSITORY / "sandbox/release-signers")
                          .read_text().splitlines() if line.strip() and not line.startswith("#"))
    assert re.search(r"# BEGIN EMBEDDED RELEASE SIGNERS\nEMBEDDED_SIGNERS=''\n# END EMBEDDED RELEASE SIGNERS\n", text)


@needs_ssh
def test_make_dist_signs_only_with_a_listed_key(scenario, tmp_path, dist, keys):
    make_dist = load(ROOT / "scripts/make_dist.py", "make_dist_for_tests")
    if scenario == "happy":
        out = tmp_path / "out"
        out.mkdir()
        (out / "SHA256SUMS").write_bytes((dist / "SHA256SUMS").read_bytes())
        assert make_dist.sign(out, keys["old"].path, [(keys["old"].kind, keys["old"].key)]) == keys["old"].fingerprint
        assert rk.verify((out / "SHA256SUMS").read_bytes(), (out / "SHA256SUMS.sig").read_bytes(),
                         rk.parse(keys["old"].line)) == ("good", keys["old"].fingerprint)
    elif scenario == "sad":
        # The repository lists no key (yet): a signature nobody could check is refused.
        result = subprocess.run([sys.executable, str(ROOT / "scripts/make_dist.py"), "--allow-dirty", "--skip-python",
                                 "--out", str(tmp_path / "out"), "--sign", str(keys["old"].path)],
                                capture_output=True, text=True, env={**os.environ, "SOURCE_DATE_EPOCH": "0"})
        assert result.returncode != 0 and "Add the key to sandbox/release-signers first" in result.stderr
        assert not (tmp_path / "out/SHA256SUMS.sig").exists()
    else:
        assert "EMBEDDED_SIGNERS=''\n" in (dist / "install.sh").read_text()  # no keys listed: nothing embedded
        with pytest.raises(SystemExit):
            make_dist.embed_signers("#!/bin/bash\n", [])


@needs_ssh
def test_publish_release_checks_the_signature_before_uploading(scenario, tmp_path, gitea, dist, keys, monkeypatch):
    source = source_of(gitea)
    files = tmp_path / "files"
    files.mkdir()
    for name in ("SHA256SUMS", "install.sh"):
        (files / name).write_bytes((dist / name).read_bytes())
    args = [sys.executable, str(ROOT / "scripts/publish_release.py"), "--source", source, "--token-env",
            "RELEASE_TOKEN", "--partial"]
    env = {**os.environ, "RELEASE_TOKEN": TOKEN}
    if scenario == "happy":
        # With keys listed (as once the maintainer registered one): --sign makes SHA256SUMS.sig, checked.
        publish = load(ROOT / "scripts/publish_release.py", "publish_release_for_tests")
        real = publish.release_keys()
        monkeypatch.setattr(publish, "release_keys", lambda: SimpleNamespace(
            load=lambda: [(keys["old"].kind, keys["old"].key)], sign=real.sign, verify=real.verify))
        chosen = publish.check_files([files / "SHA256SUMS", files / "install.sh"], keys["old"].path, False)
        assert [p.name for p in chosen] == ["SHA256SUMS", "install.sh", "SHA256SUMS.sig"]
        assert real.verify((files / "SHA256SUMS").read_bytes(), (files / "SHA256SUMS.sig").read_bytes(),
                           [(keys["old"].kind, keys["old"].key)])[0] == "good"
        with pytest.raises(SystemExit, match="SHA256SUMS is not signed"):
            publish.check_files([files / "SHA256SUMS", files / "install.sh"], None, False)
        with pytest.raises(SystemExit, match="not a trusted release key"):
            (files / "SHA256SUMS.sig").write_bytes(ssh_sign((files / "SHA256SUMS").read_bytes(), keys["evil"]))
            publish.check_files([files / "SHA256SUMS", files / "SHA256SUMS.sig"], None, False)
    elif scenario == "sad":
        (files / "install.sh").write_text("#!/bin/sh\necho stale\n")  # not what SHA256SUMS lists
        result = subprocess.run([*args, str(files / "SHA256SUMS"), str(files / "install.sh")],
                                capture_output=True, text=True, env=env)
        assert result.returncode != 0 and "does not match SHA256SUMS" in result.stderr
        result = subprocess.run([*args, "--sign", str(keys["old"].path), str(files / "SHA256SUMS")],
                                capture_output=True, text=True, env=env)
        assert result.returncode != 0 and "lists no key" in result.stderr
        assert gitea.auth == [] and gitea.uploads == []  # refused before the server heard anything
    else:
        # A new, unsigned SHA256SUMS takes the old signature down with it.
        gitea.files["SHA256SUMS.sig"] = b"old signature"
        result = subprocess.run([*args, str(files / "SHA256SUMS")], capture_output=True, text=True, env=env)
        assert result.returncode == 0, result.stderr
        assert "removed the old SHA256SUMS.sig" in result.stderr
        assert sum(1 for path, kind, _ in gitea.uploads if kind == "DELETE") == 2  # the old .sig and SHA256SUMS


def test_release_key_script(tmp_path):
    env = environment(tmp_path)
    script = ROOT / "scripts/release_key.sh"
    result = subprocess.run(["bash", str(script), "new", "--no-passphrase"], capture_output=True, text=True, env=env)
    if not shutil.which("ssh-keygen"):
        pytest.skip("ssh-keygen (OpenSSH) is needed")
    assert result.returncode == 0, result.stderr
    key = tmp_path / "home/.config/sandbox-ai/release-key"
    assert key.is_file() and (key.stat().st_mode & 0o777) == 0o600
    assert (key.parent.stat().st_mode & 0o777) == 0o700
    [(kind, blob)] = rk.parse(result.stdout)
    assert kind == "ssh-ed25519" and blob in (tmp_path / "home/.config/sandbox-ai/release-key.pub").read_text()
    assert "not yet in sandbox/release-signers" in result.stderr
    shown = subprocess.run(["bash", str(script), "show"], capture_output=True, text=True, env=env)
    assert shown.stdout == result.stdout
    again = subprocess.run(["bash", str(script), "new", "--no-passphrase"], capture_output=True, text=True, env=env)
    assert again.returncode != 0 and "exists already" in again.stderr
    inside = ROOT / "no-such-dir-for-keys" / "key"
    refused = subprocess.run(["bash", str(script), "new", "--no-passphrase", str(inside)], capture_output=True,
                             text=True, env=env)
    assert refused.returncode != 0 and "outside the repository" in refused.stderr
    assert not inside.parent.exists()


def test_a_version_in_use_is_kept(tmp_path):
    """An update keeps an old version a running program still uses (its command line names the folder): the
    app or a session loads modules from it later. Once nothing uses it, the next update removes it."""
    data = installed(tmp_path)
    versions = data / "versions"
    for old in ("0.0.1", "0.0.2", "0.0.3"):  # 0.0.3 stays anyway: the newest old one, for a rollback
        (versions / old / "sandbox").mkdir(parents=True)
    user = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", str(versions / "0.0.1" / "sandbox" / "main.py")])
    try:
        result = install(tmp_path, "--from", str(ROOT))
        assert result.returncode == 0, result.stderr
        assert (versions / "0.0.1").is_dir() and "kept sandbox-ai 0.0.1" in result.stderr
        assert not (versions / "0.0.2").exists() and (versions / "0.0.3").is_dir()
    finally:
        user.kill()
        user.wait()
    again = install(tmp_path, "--from", str(ROOT), "--force")
    assert again.returncode == 0, again.stderr
    assert not (versions / "0.0.1").exists()


# --- the shell start files: the command directory on PATH ---------------------------------------------------

def rc_block(text):
    return [line for line in text.splitlines() if "sandbox-ai" in line or "sandbox_ai_bin" in line]


def test_a_user_install_puts_its_commands_on_path_in_bashrc(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# my settings\nalias ll='ls -l'")  # no newline at the end
    (home / ".zshrc").write_text("setopt nobeep\n")
    result = install(tmp_path, "--from", str(ROOT))
    assert result.returncode == 0, result.stderr
    assert "PATH: " in result.stderr and "source ~/.bashrc" in result.stderr
    bashrc = (home / ".bashrc").read_text()
    assert bashrc.startswith("# my settings\nalias ll='ls -l'\n# >>> sandbox-ai >>>\n"), "the user's lines stay"
    assert bashrc.count("# >>> sandbox-ai >>>") == 1 and bashrc.rstrip().endswith("# <<< sandbox-ai <<<")
    assert "# >>> sandbox-ai >>>" in (home / ".zshrc").read_text()
    # A new shell finds the commands; a second one started from it does not get the directory twice.
    found = subprocess.run(["bash", "-i", "-c", 'command -v sandbox-ai; echo "$PATH"'], capture_output=True,
                           text=True, env=environment(tmp_path), timeout=30)
    where, path = found.stdout.split("\n")[:2]
    assert where == str(home / ".local/bin/sandbox-ai"), found.stderr
    again = subprocess.run(["bash", "-i", "-c", 'echo "$PATH"'], capture_output=True, text=True, timeout=30,
                           env={**environment(tmp_path), "PATH": path})
    assert again.stdout.strip().split(":").count(str(home / ".local/bin")) == 1
    # Installing again changes nothing; another --bin-dir replaces the block, never adds a second.
    assert install(tmp_path, "--from", str(ROOT), "--force").returncode == 0
    assert (home / ".bashrc").read_text() == bashrc
    other = tmp_path / "my bin's"
    assert install(tmp_path, "--from", str(ROOT), "--force", "--bin-dir", str(other)).returncode == 0
    text = (home / ".bashrc").read_text()
    assert text.count("# >>> sandbox-ai >>>") == 1 and str(home / ".local/bin") not in text
    quoted = subprocess.run(["bash", "-i", "-c", 'command -v sandbox-ai'], capture_output=True, text=True,
                            env=environment(tmp_path), timeout=30)
    assert quoted.stdout.strip() == str(other / "sandbox-ai"), "a path with a space and a quote, quoted"
    # Uninstalling takes the block away again, and only the block.
    assert install(tmp_path, "--uninstall", "--bin-dir", str(other)).returncode == 0
    assert (home / ".bashrc").read_text() == "# my settings\nalias ll='ls -l'\n"
    assert (home / ".zshrc").read_text() == "setopt nobeep\n"


def test_on_macos_the_block_goes_into_zshrc_and_the_bash_login_file(tmp_path):
    """macOS: zsh is the default shell and a new Mac has no ~/.zshrc; bash reads ~/.bash_profile at login. A stand-in
    uname says Darwin (the rest of the install runs as on this machine)."""
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    (fake / "uname").write_text("#!/bin/sh\n[ \"$1\" = -s ] && echo Darwin || /bin/uname \"$@\"\n")
    (fake / "uname").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bash_profile").write_text("export EDITOR=vi\n")
    result = install(tmp_path, "--from", str(ROOT), PATH=f"{fake}:/usr/bin:/bin", SHELL="/bin/zsh")  # (the default)
    assert result.returncode == 0, result.stderr
    assert "# >>> sandbox-ai >>>" in (home / ".zshrc").read_text(), "made, as a new Mac has none"
    assert (home / ".bash_profile").read_text().startswith("export EDITOR=vi\n# >>> sandbox-ai >>>")
    assert not (home / ".bashrc").exists(), "not made on macOS (bash there reads ~/.bash_profile)"
    assert "source ~/.zshrc" in result.stderr
    # Uninstalling leaves the user's own lines.
    assert install(tmp_path, "--uninstall", PATH=f"{fake}:/usr/bin:/bin").returncode == 0
    assert (home / ".bash_profile").read_text() == "export EDITOR=vi\n"


def test_old_versions_are_trimmed_without_gnu_head(tmp_path):
    """The rollback keeps KEEP_VERSIONS versions with awk (macOS' head has no -n -N); an empty list is fine."""
    for index in range(1, 4):
        assert install(tmp_path, "--from", str(ROOT), "--force").returncode == 0
    versions = sorted(p.name for p in (installed(tmp_path) / "versions").iterdir())
    assert len(versions) >= 1 and (installed(tmp_path) / "current").is_symlink()
    assert os.readlink(installed(tmp_path) / "current") == f"versions/{versions[-1]}"


def test_zsh_reads_the_block_too(tmp_path):
    zsh = shutil.which("zsh")
    if not zsh:
        pytest.skip("no zsh here")
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("")
    assert install(tmp_path, "--from", str(ROOT)).returncode == 0
    found = subprocess.run([zsh, "-i", "-c", "command -v sandbox-ai"], capture_output=True, text=True,
                           env=environment(tmp_path), timeout=30)
    assert found.stdout.strip() == str(home / ".local/bin/sandbox-ai")


def test_tab_completion_in_every_shell(tmp_path):
    """The install writes the completion scripts where bash, zsh and fish find them; a new interactive bash and
    zsh have it at once (the rc block), fish loads its file by itself; --uninstall takes them away."""
    home = tmp_path / "home"
    home.mkdir()
    (home / ".zshrc").write_text("autoload -U compinit && compinit -u\n")
    result = install(tmp_path, "--from", str(ROOT))
    assert result.returncode == 0, result.stderr
    assert "Tab completion for bash, zsh and fish" in result.stderr
    data, config = home / ".local/share", home / ".config"
    files = [data / "bash-completion/completions/sandbox-ai", data / "zsh/site-functions/_sandbox-ai",
             config / "fish/completions/sandbox-ai.fish"]
    assert all("sandbox-ai __complete" in f.read_text() for f in files)
    found = subprocess.run(["bash", "-i", "-c", "complete -p sandbox-ai"], capture_output=True, text=True,
                           env=environment(tmp_path), timeout=60)
    assert "_sandbox_ai sandbox-ai" in found.stdout, found.stderr
    zsh = os.environ.get("SANDBOX_AI_TEST_ZSH") or shutil.which("zsh")
    if zsh:
        # A relocated zsh (a static build) finds its own functions (compinit) only through FPATH.
        own = sorted(Path(zsh).resolve().parents[1].glob("share/zsh/*/functions"))
        env = {**environment(tmp_path), **({"FPATH": str(own[0])} if own else {})}
        found = subprocess.run([zsh, "-i", "-c", "print -r -- ${_comps[sandbox-ai]}"], capture_output=True,
                               text=True, env=env, timeout=60)
        assert found.stdout.strip() == "_sandbox_ai", (found.stdout, found.stderr)
    assert install(tmp_path, "--uninstall").returncode == 0
    assert not any(f.exists() for f in files)


def test_the_end_says_how_a_shell_that_runs_gets_it(tmp_path):
    """A program cannot change a shell that already runs: the end of an install says how a terminal open now gets
    the PATH and the Tab completion (source the rc file), also when the command directory is on PATH already and only
    the completion is new; an install that changed nothing says nothing of it."""
    home = tmp_path / "home"
    home.mkdir()
    on_path = f"{home / '.local/bin'}:/usr/bin:/bin"
    result = install(tmp_path, "--from", str(ROOT), PATH=on_path)
    assert result.returncode == 0, result.stderr
    assert "source ~/.bashrc" in result.stderr and "cannot change a shell that already runs" in result.stderr
    again = install(tmp_path, "--from", str(ROOT), "--force", PATH=on_path)
    assert again.returncode == 0 and "source ~/.bashrc" not in again.stderr, again.stderr
    assert "Tab completion" not in again.stderr, "unchanged: not said again"
    # the completion gone (removed by hand): written again, and said how this shell gets it
    (home / ".local/share/bash-completion/completions/sandbox-ai").unlink()
    third = install(tmp_path, "--from", str(ROOT), "--force", PATH=on_path, SHELL="/bin/zsh")
    assert (home / ".local/share/bash-completion/completions/sandbox-ai").is_file()
    assert "Tab completion" in third.stderr and "source ~/.bashrc" in third.stderr, third.stderr
    # --no-modify-rc: the completion file itself is what a running shell sources
    (tmp_path / "other").mkdir()
    plain = install(tmp_path / "other", "--from", str(ROOT), "--no-modify-rc", PATH=on_path)
    assert plain.returncode == 0, plain.stderr
    assert "source ~/.local/share/bash-completion/completions/sandbox-ai" in plain.stderr, plain.stderr
    assert not (tmp_path / "other/home/.bashrc").exists()


def test_an_up_to_date_update_puts_the_block_and_completion_back(tmp_path, gitea):
    """sandbox-ai update with nothing newer still refreshes the rc block and the Tab completion (a block removed by
    hand, a completion of an older version)."""
    source = f"http://127.0.0.1:{gitea.server_address[1]}/o/r"
    assert install(tmp_path, "--source", source).returncode == 0
    home = tmp_path / "home"
    (home / ".bashrc").write_text("# mine\n")
    (home / ".local/share/bash-completion/completions/sandbox-ai").write_text("# old\n")
    again = install(tmp_path, "--source", source)
    assert again.returncode == 0 and "up to date" in again.stderr, again.stderr
    assert "# >>> sandbox-ai >>>" in (home / ".bashrc").read_text()
    assert "sandbox-ai __complete" in (home / ".local/share/bash-completion/completions/sandbox-ai").read_text()
    assert "source ~/.bashrc" in again.stderr


def test_a_system_install_puts_tab_completion_where_the_shells_look(tmp_path):
    """--system: the scripts under PREFIX/share, where bash-completion, zsh and fish look (no rc file of anyone is
    changed); --uninstall takes them away."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    env = environment(tmp_path)
    done = subprocess.run(["bash", str(ROOT / "install.sh"), "--system", "--prefix", str(prefix), "--from", str(ROOT),
                           "--no-app"], capture_output=True, text=True, env=env, timeout=300)
    assert done.returncode == 0, done.stderr
    files = [prefix / "share/bash-completion/completions/sandbox-ai", prefix / "share/zsh/site-functions/_sandbox-ai",
             prefix / "share/fish/vendor_completions.d/sandbox-ai.fish"]
    assert all("sandbox-ai __complete" in f.read_text() for f in files), done.stderr
    assert all(oct(f.stat().st_mode & 0o777) == "0o644" for f in files)
    assert not (tmp_path / "home/.bashrc").exists()
    assert "source " + str(files[0]) in done.stderr
    found = subprocess.run(["bash", "-c", f". {files[0]}; complete -p sandbox-ai"], capture_output=True, text=True,
                           env=env, timeout=60)
    assert "_sandbox_ai sandbox-ai" in found.stdout, found.stderr
    gone = subprocess.run(["bash", str(ROOT / "install.sh"), "--system", "--prefix", str(prefix), "--uninstall"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert gone.returncode == 0 and not any(f.exists() for f in files), gone.stderr


def test_any_user_may_check_a_system_installation_and_only_root_changes_it(tmp_path):
    """CI 0.1.186: a user's `sandbox-ai update --check` of a system installation (root's, /usr/local: they cannot
    write it) runs install.sh --check --system --prefix P - which said "--system needs root" before it checked
    anything, so the app's and notify's daily check failed for every user but root. --check only reads; installing,
    fetching and uninstalling there still need root (or a prefix the user may write). (A fake `id` says uid 1000 and
    the prefix is not there, so not writable - as root too.)"""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "id").write_text('#!/bin/sh\n[ "$1" = "-u" ] && { echo 1000; exit 0; }\nexec /usr/bin/id "$@"\n')
    (fake / "id").chmod(0o755)
    prefix = tmp_path / "usr-local"  # (not there: -w is false for root too)
    env = environment(tmp_path, PATH=f"{fake}:/usr/bin:/bin")

    def run(*args):
        return subprocess.run(["bash", str(ROOT / "install.sh"), "--system", "--prefix", str(prefix), *args],
                              capture_output=True, text=True, env=env, timeout=120)
    check = run("--check", "--from", str(ROOT))
    assert check.returncode == 0, check.stderr
    assert f"installed: none ({prefix}/lib/sandbox-ai)" in check.stdout and "needs root" not in check.stderr
    for args in (["--from", str(ROOT), "--no-app"], ["--uninstall"], ["--fetch", str(tmp_path / "fetched")]):
        refused = run(*args)
        assert refused.returncode != 0 and "--system needs root" in refused.stderr, (args, refused.stderr)
    # --uninstall --dry-run only lists (sandbox-ai hosts uninstall shows it before sudo asks for a password)
    listed = run("--uninstall", "--dry-run")
    assert listed.returncode == 0 and "needs root" not in listed.stderr, listed.stderr
    assert not prefix.exists() and not (tmp_path / "fetched").exists()


def test_install_sh_colours_on_a_terminal_only(tmp_path):
    """install.sh says its errors red on a terminal (FORCE_COLOR stands in for one here), never with NO_COLOR or
    --no-color, never in a pipe."""
    bad = ("--version", "nonsense")
    forced = install(tmp_path, *bad, FORCE_COLOR="1")
    assert forced.returncode == 1 and "\033[31m" in forced.stderr and "--version expects X.Y.Z" in forced.stderr
    for extra, env in ((("--no-color",), {"FORCE_COLOR": "1"}), ((), {"FORCE_COLOR": "1", "NO_COLOR": "1"}),
                       ((), {})):
        plain = install(tmp_path, *extra, *bad, **env)
        assert plain.returncode == 1 and "\033[" not in plain.stderr, (extra, env)
        assert "install.sh: --version expects X.Y.Z" in plain.stderr


def test_the_files_can_be_left_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    result = install(tmp_path, "--from", str(ROOT), "--no-modify-rc")
    assert result.returncode == 0, result.stderr
    assert not (home / ".bashrc").exists(), "no file of its own made either"
    assert "add " in result.stderr and "to your PATH" in result.stderr, "the hint instead"


def test_a_linked_bashrc_stays_a_link(tmp_path):
    home = tmp_path / "home"
    (tmp_path / "dotfiles").mkdir()
    home.mkdir()
    (tmp_path / "dotfiles/bashrc").write_text("export EDITOR=vim\n")
    (home / ".bashrc").symlink_to(tmp_path / "dotfiles/bashrc")
    assert install(tmp_path, "--from", str(ROOT)).returncode == 0
    assert (home / ".bashrc").is_symlink() and "# >>> sandbox-ai >>>" in (tmp_path / "dotfiles/bashrc").read_text()


def test_a_broken_block_is_never_rewritten(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".bashrc").write_text("# >>> sandbox-ai >>>\nexport X=1\n")  # an end marker lost by hand
    result = install(tmp_path, "--from", str(ROOT))
    assert result.returncode != 0 or "fix it by hand" in result.stderr
    assert (home / ".bashrc").read_text() == "# >>> sandbox-ai >>>\nexport X=1\n"


def test_uninstall_keeps_the_users_data_beside_the_installation(tmp_path):
    """In a user install the data directory also holds the secrets, logins, kits, plugins, VPN keys and MCP rootfs
    trees: --uninstall removes the installation (versions, current, the app's environment, the release state) only."""
    assert install(tmp_path, "--from", str(ROOT)).returncode == 0
    data = installed(tmp_path)
    kept = {"secrets/GITEA_TOKEN": "token", "auth/claude/.claude/.credentials.json": "{}", "kits/mine/kit.json": "{}",
            "vpn/home.key": "key", "mcp-rootfs/abc/README": "x", "home/claude/CLAUDE.md": "notes"}
    for relative, text in kept.items():
        (data / relative).parent.mkdir(parents=True, exist_ok=True)
        (data / relative).write_text(text)
    readonly = data / "mcp-rootfs" / "abc"
    readonly.chmod(0o555)
    try:
        result = install(tmp_path, "--uninstall")
        assert result.returncode == 0, result.stderr
        assert not (data / "versions").exists() and not (data / "current").exists()
        assert not (data / "release-state.json").exists()
        for relative, text in kept.items():
            assert (data / relative).read_text() == text, relative
        assert "your data there (secrets, logins, kits" in result.stderr
    finally:
        readonly.chmod(0o755)


def installer_helper():
    """install.sh's Python helper (its heredoc) as a module, without running its command dispatch."""
    import types
    text = (ROOT / "install.sh").read_text()
    code = text.split("python3 -I - \"$@\" <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    code = code.split('\nrun_command(sys.argv)', 1)[0]  # without the command's run (run_command(sys.argv))
    module = types.ModuleType("install_helper")
    exec(compile(code, "install.sh helper", "exec"), module.__dict__)
    return module


def test_a_redirect_never_carries_the_token_over_plain_http_or_to_another_host():
    import urllib.request
    helper = installer_helper()
    handler = helper.SameHostRedirect()
    def follow(old, new):
        request = urllib.request.Request(old, headers={"Authorization": "token secret-token-123"})
        return handler.redirect_request(request, None, 302, "Found", {}, new)
    assert follow("https://git.example/a", "https://git.example/b").get_header("Authorization") == "token secret-token-123"
    assert follow("https://git.example/a", "http://git.example/b").get_header("Authorization") is None, "plain http"
    assert follow("https://git.example/a", "https://other.example/b").get_header("Authorization") is None
    assert follow("http://localhost:3080/a", "http://localhost:3080/b").get_header("Authorization") == \
        "token secret-token-123"  # this host's loopback may be plain http


def test_the_mcp_catalog_ships_with_its_signature_and_rootfs(tmp_path):
    """A signed catalog without its .sig (and rootfs.json with its own) would read as unsigned once installed."""
    helper = installer_helper()
    checkout, target = tmp_path / "checkout", tmp_path / "target"
    mcp = checkout / "projects" / "sandbox-ai-mcp"
    mcp.mkdir(parents=True)
    for name in ("catalog.json", "catalog.json.sig", "rootfs.json", "rootfs.json.sig", "README.md"):
        (mcp / name).write_text(name)
    (checkout / "sandbox").mkdir()
    (checkout / "sandbox" / "x.py").write_text("x")
    (checkout / ".gitignore").write_text("projects/\n")  # as in a checkout: the other repositories are not its files
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    target.mkdir()
    helper.cmd_copy(str(checkout), str(target))
    shipped = sorted(p.name for p in (target / "projects" / "sandbox-ai-mcp").iterdir())
    assert shipped == ["catalog.json", "catalog.json.sig", "rootfs.json", "rootfs.json.sig"], shipped
    import importlib.util
    spec = importlib.util.spec_from_file_location("make_dist_test", ROOT / "scripts/make_dist.py")
    dist = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dist)
    assert set(dist.MCP_CATALOG_FILES) >= {"projects/sandbox-ai-mcp/catalog.json.sig", "projects/sandbox-ai-mcp/rootfs.json"}


def test_copying_an_upload_folder_never_runs_git_of_a_folder_above(tmp_path):
    """Review 5 L4: `hosts install` uploads a tree without .git and install.sh ran `git ls-files` in it; git then
    used a repository of a folder above (/tmp/.git, whose core.fsmonitor runs a command). Only the checkout's own
    repository is asked; without one every file is copied."""
    helper = installer_helper()
    above = tmp_path / "above"
    upload = above / "sandbox-ai-install.abc"
    (upload / "sandbox").mkdir(parents=True)
    (upload / "sandbox" / "x.py").write_text("x")
    subprocess.run(["git", "init", "-q", str(above)], check=True)
    marker = tmp_path / "ran"
    subprocess.run(["git", "-C", str(above), "config", "core.fsmonitor", f"touch {marker}; false"], check=True)
    target = tmp_path / "target"
    target.mkdir()
    helper.cmd_copy(str(upload), str(target))
    assert (target / "sandbox" / "x.py").read_text() == "x" and not marker.exists()


def test_install_from_a_checkout_of_another_user_skips_sockets_and_ignored_files(tmp_path, monkeypatch):
    """Live use (2026-11-14), `./install.sh --from .` as root in the user's checkout: "OSError: [Errno 6] No such
    device or address: 'CHECKOUT/.tt/core/pytest-of-root/.../open.sock'". git refused the
    checkout of another user (dubious ownership), the copy fell back to every file, ignored ones and a socket too.
    git is asked with the checkout as a safe directory, and a fallback copies regular files only."""
    import socket
    helper = installer_helper()
    checkout, target = tmp_path / "c", tmp_path / "target"
    (checkout / "sandbox").mkdir(parents=True)
    (checkout / "sandbox" / "x.py").write_text("x")
    (checkout / ".gitignore").write_text(".tt/\n")
    subprocess.run(["git", "init", "-q", str(checkout)], check=True)
    subprocess.run(["git", "-C", str(checkout), "add", "-A"], check=True)
    (checkout / ".tt").mkdir()
    (checkout / ".tt" / "big.log").write_text("ignored")
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(checkout / ".tt" / "s.sock"))
    os.mkfifo(checkout / "sandbox" / "pipe")
    # git is told the checkout is safe (root in a user's checkout: else "dubious ownership" and no listing).
    asked = []
    real_run = subprocess.run

    def run(command, *args, **kwargs):
        asked.append(command)
        return real_run(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, "run", run)
    target.mkdir()
    helper.cmd_copy(str(checkout), str(target))
    monkeypatch.setattr(subprocess, "run", real_run)
    assert any("safe.directory=" + str(checkout) in " ".join(map(str, c)) for c in asked if "ls-files" in c)
    assert (target / "sandbox" / "x.py").read_text() == "x"
    assert not (target / ".tt").exists(), "git's listing was used: ignored files stay out"
    assert not (target / "sandbox" / "pipe").exists()
    # git failing (a broken repository, or a git that refuses): the fallback copies regular files only.
    import shutil as sh
    sh.rmtree(checkout / ".git")
    (checkout / ".git").write_text("gitdir: /nonexistent\n")
    sh.rmtree(target)
    target.mkdir()
    helper.cmd_copy(str(checkout), str(target))
    assert (target / "sandbox" / "x.py").exists() and not (target / ".tt" / "s.sock").exists()
    assert not (target / "sandbox" / "pipe").exists()
    server.close()


# --- the end of an installation: what still keeps the human on another version (2026-11-18: "I ran install.sh --from
# . several times, but when I start sandbox-ai-app it says 0.1.97": a system installation's 0.1.97 came first in PATH,
# and its app ran) ---

def fake_system_installation(prefix, version="0.1.97"):
    """PREFIX/lib/sandbox-ai/versions/VERSION with its command links in PREFIX/bin, as install.sh --system makes it."""
    data = prefix / "lib" / "sandbox-ai"
    tree = data / "versions" / version
    (tree / "sandbox").mkdir(parents=True)
    (tree / "sandbox" / "agent_sandbox.py").write_text(f'__version__ = "{version}"\n')
    for name in ("sandbox-ai", "sandbox-ai-app", "install.sh"):
        (tree / name).write_text("#!/bin/sh\necho old\n")
        (tree / name).chmod(0o755)
    (data / "current").symlink_to(f"versions/{version}")
    (prefix / "bin").mkdir(parents=True)
    for command, target in (("sandbox-ai", "sandbox-ai"), ("sandbox-ai-app", "sandbox-ai-app"),
                            ("sandbox-ai-update", "install.sh")):
        (prefix / "bin" / command).symlink_to(data / "current" / target)


def lines_after(stderr, marker):
    lines = stderr.splitlines()
    start = next(i for i, line in enumerate(lines) if marker in line)
    return lines[start:]


@needs_app
def test_a_system_installation_first_in_path_is_named_at_the_end(tmp_path):
    """User mode: each command a new shell's PATH finds in another installation first is named, with both versions and
    the two ways out (remove it or update it); the user's own first in PATH: nothing."""
    prefix = tmp_path / "prefix"
    fake_system_installation(prefix)
    home_bin = tmp_path / "home/.local/bin"
    result = install(tmp_path, "--from", str(ROOT), PATH=f"{prefix / 'bin'}:/usr/bin:/bin:{home_bin}")
    assert result.returncode == 0, result.stderr
    end = "\n".join(lines_after(result.stderr, f"installed sandbox-ai {VERSION}"))
    assert (f"warning: your PATH finds sandbox-ai, sandbox-ai-update, sandbox-ai-app in {prefix / 'bin'} first - "
            f"sandbox-ai 0.1.97 (a system installation in {prefix})") in end, result.stderr
    assert f"before the {VERSION} installed now in {home_bin}" in end
    assert f"sudo {prefix}/lib/sandbox-ai/current/install.sh --uninstall --system --prefix {prefix}" in end
    assert f"sudo {ROOT}/install.sh --from {ROOT} --system --prefix {prefix}" in end
    # ~/.local/bin not in PATH yet: the rc block puts it first - a new shell finds this installation.
    again = install(tmp_path, "--from", str(ROOT), "--force", PATH=f"{prefix / 'bin'}:/usr/bin:/bin")
    assert again.returncode == 0 and "your PATH finds" not in again.stderr, again.stderr
    first = install(tmp_path, "--from", str(ROOT), PATH=f"{home_bin}:{prefix / 'bin'}:/usr/bin:/bin")
    assert first.returncode == 0 and "your PATH finds" not in first.stderr, first.stderr


@needs_app
def test_a_link_left_out_for_a_foreign_file_is_said_again_at_the_end(tmp_path):
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "sandbox-ai-app").write_text("#!/bin/sh\necho mine\n")
    (bin_dir / "sandbox-ai-app").chmod(0o755)
    result = install(tmp_path, "--from", str(ROOT), PATH=f"{bin_dir}:/usr/bin:/bin")
    assert result.returncode == 0, result.stderr
    end = "\n".join(lines_after(result.stderr, f"installed sandbox-ai {VERSION}"))
    assert f"warning: not linked: {bin_dir / 'sandbox-ai-app'} exists and does not belong to sandbox-ai" in end
    assert f"your PATH finds sandbox-ai-app in {bin_dir} first - a file not sandbox-ai's" in end
    assert (bin_dir / "sandbox-ai-app").read_text() == "#!/bin/sh\necho mine\n"


class AppSocket(threading.Thread):
    """The app's single-instance socket in RUNTIME: an app before 0.1.99 (ANSWER None: it closes the connection on
    "info") or one that answers "info" with ANSWER."""

    def __init__(self, runtime, answer=None):
        import socket
        super().__init__(daemon=True)
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(runtime / "sandbox-ai-app.sock"))
        self.server.listen(4)
        self.answer, self.heard = answer, []
        self.start()

    def run(self):
        while True:
            try:
                client, _ = self.server.accept()
            except OSError:
                return
            word = client.recv(64).strip()
            self.heard.append(word)
            if word == b"info" and self.answer is not None:
                client.sendall(json.dumps(self.answer).encode() + b"\n")
            client.close()

    def stop(self):
        self.server.close()


@needs_app
def test_an_app_that_runs_an_older_version_is_named_at_the_end(tmp_path):
    import tempfile
    runtime = Path(tempfile.mkdtemp(prefix="ia-"))
    try:
        result = install(tmp_path, "--from", str(ROOT), XDG_RUNTIME_DIR=str(runtime))
        assert result.returncode == 0 and "sandbox-ai-app" not in "\n".join(
            lines_after(result.stderr, "installed sandbox-ai")[1:]).replace("the app: sandbox-ai-app", ""), \
            "nothing runs: nothing said"
        old = AppSocket(runtime)
        try:
            result = install(tmp_path, "--from", str(ROOT), "--force", XDG_RUNTIME_DIR=str(runtime))
        finally:
            old.stop()
        assert result.returncode == 0, result.stderr
        assert f"warning: an older sandbox-ai-app is running (PID {os.getpid()}); it is too old to make way for " \
               f"{VERSION} by itself: quit it" in result.stderr, result.stderr
        assert f"kill {os.getpid()}" in result.stderr and old.heard == [b"info"], "asked, never shown or quit"
        (runtime / "sandbox-ai-app.sock").unlink()
        new = AppSocket(runtime, {"app": "0.1.98", "path": "/opt/app/sandbox_ai_app", "pid": 4242})
        try:
            result = install(tmp_path, "--from", str(ROOT), "--force", XDG_RUNTIME_DIR=str(runtime))
        finally:
            new.stop()
        assert (f"warning: sandbox-ai-app 0.1.98 is running (from /opt/app/sandbox_ai_app, PID 4242), not the "
                f"{VERSION} installed now: quit it, or start sandbox-ai-app again - it takes over") in result.stderr
        (runtime / "sandbox-ai-app.sock").unlink()
        same = AppSocket(runtime, {"app": VERSION, "path": str(installed(tmp_path) / "current/projects/sandbox-ai-app/"
                                                                "sandbox_ai_app"), "pid": 4242})
        try:
            result = install(tmp_path, "--from", str(ROOT), XDG_RUNTIME_DIR=str(runtime))
        finally:
            same.stop()
        assert result.returncode == 0 and "is running" not in result.stderr, result.stderr
    finally:
        shutil.rmtree(runtime, ignore_errors=True)


def test_as_root_the_invoking_users_own_installation_is_named(tmp_path):
    """install.sh --system through sudo (or a root login in the user's checkout): the user's own installation in
    ~/.local/share/sandbox-ai stays at its version - said, with how to update it as that user."""
    helper = installer_helper()
    home = tmp_path / "alice"
    (home / ".local/share/sandbox-ai/current/sandbox").mkdir(parents=True)
    (home / ".local/share/sandbox-ai/current/sandbox/agent_sandbox.py").write_text('__version__ = "0.1.97"\n')
    entry = SimpleNamespace(pw_name="alice", pw_dir=str(home), pw_uid=1234)
    users = {"alice": entry}
    note = helper.user_note(".", "0.1.99", {"SUDO_USER": "alice"}, users.__getitem__)
    assert note == (f"warning: alice has an installation of their own, sandbox-ai 0.1.97 in "
                    f"{home}/.local/share/sandbox-ai: it stays at 0.1.97, and their PATH may find it first (~/.local/bin); update it as alice: "
                    f"./install.sh --from . (or remove it as alice: {home}/.local/share/sandbox-ai/current/install.sh "
                    "--uninstall)")
    assert helper.user_note(".", "0.1.97", {"SUDO_USER": "alice"}, users.__getitem__) is None, "the same version"
    assert helper.user_note(".", "0.1.99", {"SUDO_USER": "bob"}, users.__getitem__) is None, "no such user"
    assert helper.user_note(".", "0.1.99", {}, users.__getitem__, lambda path: 0) is None, "root's own checkout"
    assert helper.invoking_user(".", {"SUDO_USER": "alice"}, users.__getitem__) == ("alice", str(home), 1234)
    import pwd
    try:
        owner = pwd.getpwuid(os.getuid() or 1).pw_name if os.getuid() else None
    except KeyError:
        owner = None
    if owner:  # a root login in a user's checkout: the checkout's owner (a non-root run: its own user)
        assert helper.invoking_user(".", {}, users.__getitem__, lambda path: os.getuid()) is None or owner


def test_a_system_installation_through_sudo_ends_cleanly(tmp_path):
    """The notes of a system installation run (bash and its helper): a SUDO_USER without an installation of their own,
    or none at all, says nothing about one."""
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    for extra in ({"SUDO_USER": "nobody-such-user-here"}, {}):
        done = subprocess.run(["bash", str(ROOT / "install.sh"), "--system", "--prefix", str(prefix), "--from",
                               str(ROOT), "--no-app", "--force"], capture_output=True, text=True,
                              env=environment(tmp_path, **extra), timeout=300)
        assert done.returncode == 0, done.stderr
        assert "installation of their own" not in done.stderr and "Traceback" not in done.stderr


# --- install.sh --fetch and the update mode, end to end (sandbox/updates.py) ----------------------------------------

def fetched(tmp_path):
    return installed(tmp_path) / "update-pending"


@needs_ssh
def test_fetch_downloads_and_checks_a_release_without_installing_it(scenario, tmp_path, gitea, dist, keys):
    trusting(tmp_path, gitea, dist, keys["old"])
    folder = fetched(tmp_path)
    installer = installed(tmp_path) / "current/install.sh"
    run = lambda *args: subprocess.run(["bash", str(installer), "--user", "--source", source_of(gitea), *args],
                                       capture_output=True, text=True, env=environment(tmp_path), timeout=120)
    if scenario == "happy":
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["old"]))
        result = run("--fetch", str(folder))
        assert result.returncode == 0, result.stderr
        assert result.stdout.splitlines() == ["9.2.0", f"good {keys['old'].fingerprint}",
                                              "sandbox-ai-bundle-9.2.0.tar.gz"]
        assert sorted(p.name for p in folder.iterdir()) == ["SHA256SUMS", "SHA256SUMS.sig",
                                                            "sandbox-ai-bundle-9.2.0.tar.gz"]
        assert current(tmp_path) == "9.1.0", "fetched, not installed"
        # Installed later from the folder, checked again (and the tree must be the version its name says).
        result = install(tmp_path, "--from", str(folder / "sandbox-ai-bundle-9.2.0.tar.gz"), "--verify")
        assert result.returncode == 0 and current(tmp_path) == "9.2.0", result.stderr
    elif scenario == "sad":
        # Signed by a key nobody trusts, or not signed: refused whatever --allow-unsigned says; nothing kept.
        gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["evil"]))
        result = run("--fetch", str(folder), "--allow-unsigned")
        assert result.returncode != 0 and "not a trusted release key; nothing downloaded" in result.stderr
        gitea.publish("9.2.1", build_release(dist, "9.2.1", [keys["old"]], None))
        result = run("--fetch", str(folder))
        assert result.returncode != 0 and "is not signed" in result.stderr
        assert not folder.exists() and current(tmp_path) == "9.1.0"
    else:
        # Never older than the installed version; a renamed bundle whose tree is another version is refused.
        gitea.publish("9.0.0", build_release(dist, "9.0.0", [keys["old"]], keys["old"]))
        result = run("--fetch", str(folder))
        assert result.returncode != 0 and "older than the installed 9.1.0" in result.stderr
        assert not folder.exists()
        release = build_release(dist, "9.3.0", [keys["old"]], keys["old"])
        renamed = {"sandbox-ai-bundle-9.4.0.tar.gz": release["sandbox-ai-bundle-9.3.0.tar.gz"]}
        renamed["SHA256SUMS"] = (f"{hashlib.sha256(renamed['sandbox-ai-bundle-9.4.0.tar.gz']).hexdigest()}  "
                                 "sandbox-ai-bundle-9.4.0.tar.gz\n").encode()
        renamed["SHA256SUMS.sig"] = ssh_sign(renamed["SHA256SUMS"], keys["old"])
        folder.mkdir()
        for name, data in renamed.items():
            (folder / name).write_bytes(data)
        result = install(tmp_path, "--from", str(folder / "sandbox-ai-bundle-9.4.0.tar.gz"), "--verify")
        assert result.returncode != 0 and "the bundle of release 9.4.0 contains sandbox-ai 9.3.0" in result.stderr
        assert current(tmp_path) == "9.1.0"


def fake_timer_tools(tmp_path):
    """systemctl, launchctl and notify-send that only record their arguments: no real user manager or desktop."""
    folder = tmp_path / "fakebin"
    folder.mkdir(exist_ok=True)
    for name in ("systemctl", "notify-send", "launchctl"):
        (folder / name).write_text(f'#!/bin/sh\necho "{name} $*" >> "{tmp_path}/fake.log"\nexit 0\n')
        (folder / name).chmod(0o755)
    return folder


@needs_ssh
def test_update_mode_notify_download_apply_and_rollback_through_sandbox_ai(tmp_path, gitea, dist, keys):
    """The whole through the installed ./sandbox-ai, against the fake release server: notify (the timer made, the
    day's check, the event "update" to a notify channel), download (fetched and checked, nothing switched), --apply,
    --rollback, mode off (the timer gone)."""
    trusting(tmp_path, gitea, dist, keys["old"])
    gitea.publish("9.2.0", build_release(dist, "9.2.0", [keys["old"]], keys["old"]))
    base = environment(tmp_path)
    base["PATH"] = f"{fake_timer_tools(tmp_path)}:{base['PATH']}"
    command = tmp_path / "home/.local/bin/sandbox-ai"
    at = 1800000000  # (a fixed clock: SANDBOX_AI_UPDATE_NOW)

    def sai(*args, when=at):
        return subprocess.run([str(command), *args], capture_output=True, text=True, timeout=300,
                              env={**base, "SANDBOX_AI_UPDATE_NOW": str(when)})

    shown = sai("update", "mode")
    assert shown.returncode == 0 and "mode: notify (the default)" in shown.stdout, shown.stderr
    config = tmp_path / "home/.config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "defaults.json").write_text(json.dumps({"version": 1, "desktop_notifications": "none"}))
    hook = f"http://127.0.0.1:{gitea.server_address[1]}/hook"
    added = sai("notify", "add", "phone", "--webhook", hook, "--events", "update", "--when", "always", "--rate", "0")
    assert added.returncode == 0, added.stderr
    made = sai("update", "mode", "notify", "--source", source_of(gitea))
    assert made.returncode == 0, made.stderr
    units = tmp_path / "home/.config/systemd/user"
    service = (units / "sandbox-ai-update.service").read_text()
    assert f'"{installed(tmp_path)}/current/sandbox/main.py" "update" "--scheduled"' in service
    assert "systemctl --user enable --now sandbox-ai-update.timer" in (tmp_path / "fake.log").read_text()

    checked = sai("update", "--scheduled")
    assert checked.returncode == 0, checked.stderr
    assert "checked: 9.2.0 is available (installed: 9.1.0), signed by" in checked.stdout
    assert any(event["event"] == "update" and "9.2.0 is available" in event["what"]
               for body in gitea.hooks for event in body["events"]), gitea.hooks
    asked = sum(path.endswith("/releases/latest") for path, _ in gitea.auth)
    again = sai("update", "--scheduled", when=at + 3600)
    assert again.returncode == 0 and sum(path.endswith("/releases/latest") for path, _ in gitea.auth) == asked, \
        "once a day"
    status = json.loads(sai("update", "mode", "--json", when=at + 3600).stdout)
    assert status["result"]["available"] == "9.2.0" and status["result"]["newer"] is True
    assert status["timer"]["installed"] is True and status["kind"] == "user"
    assert status["next_check"] == at + 86400

    switched = sai("update", "mode", "download", when=at + 3600)
    assert switched.returncode == 0, switched.stderr
    downloaded = sai("update", "--scheduled", when=at + 3600)
    assert downloaded.returncode == 0 and "downloaded and checked: 9.2.0" in downloaded.stdout, downloaded.stderr
    assert (fetched(tmp_path) / "sandbox-ai-bundle-9.2.0.tar.gz").is_file() and current(tmp_path) == "9.1.0"
    assert "downloaded: 9.2.0" in sai("update", "mode").stdout

    applied = sai("update", "--apply")
    assert applied.returncode == 0, applied.stderr
    assert "installed 9.2.0 (was 9.1.0)" in applied.stdout and current(tmp_path) == "9.2.0"
    assert f"by {keys['old'].fingerprint}" in applied.stderr, "checked again before it was installed"
    assert not fetched(tmp_path).exists()

    back = sai("update", "--rollback")
    assert back.returncode == 0, back.stderr
    assert "back to 9.1.0 (was 9.2.0)" in back.stdout and current(tmp_path) == "9.1.0"
    nothing = sai("update", "--scheduled", when=at + 2 * 86400)
    assert "downloaded" not in nothing.stdout and not fetched(tmp_path).exists(), "the version left is not taken again"

    off = sai("update", "mode", "off")
    assert off.returncode == 0 and not (units / "sandbox-ai-update.timer").exists(), off.stderr
    assert "systemctl --user disable --now sandbox-ai-update.timer" in (tmp_path / "fake.log").read_text()


@pytest.mark.parametrize("error, said", [
    (OSError(28, "No space left on device", "/tmp/w/bundle.tar.gz"), "the disk is full (/tmp/w/bundle.tar.gz)"),
    (OSError(30, "Read-only file system", "/usr/local/lib/sandbox-ai"),
     "the file system is read-only (/usr/local/lib/sandbox-ai)"),
    (PermissionError(13, "Permission denied", "/usr/local/lib/sandbox-ai/versions"),
     "permission denied (/usr/local/lib/sandbox-ai/versions)"),
])
def test_what_the_system_refuses_is_one_line_never_a_traceback(error, said):
    """A full disk, a read-only prefix, a folder of someone else's during an update or a fetch: install.sh says it in
    one line and switches nothing (it ended in a Python traceback)."""
    helper = installer_helper()

    def refused(*args):
        raise error
    helper.cmd_download = refused
    with pytest.raises(SystemExit) as ended:
        helper.run_command(["-", "download", "https://github.com/c0decave/sandbox-ai"])
    assert ended.value.code == f"install.sh: {said}; nothing installed"


def test_a_release_seen_at_one_source_is_no_reason_to_distrust_another(tmp_path, capsys):
    """The user's system installation (2026-11-22): 0.1.193 from the local Gitea, checked against GitHub, whose
    newest release is 0.1.188 - "an old release served again?" and a failed update. Seen releases are kept per
    source: another source's older newest is "nothing to update" (status 3, said plainly); the same source's older
    newest after a newer one of its own is still refused as a replay."""
    helper = installer_helper()
    state = tmp_path / "release-state.json"
    gitea, github = "http://gitea.local:3000/u/sandbox-ai", "https://github.com/c0decave/sandbox-ai"
    helper.cmd_record(str(state), "0.1.193", gitea + ".git")
    assert helper.highest_seen(str(state), gitea) == "0.1.193" and helper.highest_seen(str(state), github) == ""
    with pytest.raises(SystemExit) as ended:
        helper.check_version("0.1.188", "0.1.193", str(state), "0", False, github)
    assert ended.value.code == helper.NEWER_INSTALLED
    said = capsys.readouterr().err
    assert "sandbox-ai 0.1.193 is newer than the newest release 0.1.188: nothing to update" in said
    with pytest.raises(SystemExit) as ended:
        helper.check_version("0.1.190", "0.1.193", str(state), "0", False, gitea)
    assert "has seen 0.1.193 there before (an old release served again?)" in str(ended.value.code)
    helper.cmd_record(str(state), "0.1.188", github)
    document = json.loads(state.read_text())
    assert document["highest_seen"] == "0.1.193" and document["by_source"] == {gitea: "0.1.193", github: "0.1.188"}
    # a state of before (no sources): its newest is said, the update not refused for it
    state.write_text(json.dumps({"highest_seen": "0.1.193"}))
    with pytest.raises(SystemExit) as ended:
        helper.check_version("0.1.188", "0.1.193", str(state), "0", False, github)
    assert ended.value.code == helper.NEWER_INSTALLED and "has seen 0.1.193 before" in capsys.readouterr().err
