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
import subprocess
import sys
import tarfile
import threading
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = re.search(r'^__version__ = "([^"]+)"$', (ROOT / "sandbox/agent_sandbox.py").read_text(), re.M)[1]
COMMANDS = ["sandbox-ai", "claude-sandbox", "codex-sandbox", "opencode-sandbox", "sandbox-session", "sandbox-mcp",
            "sandbox-wizard", "sandbox-build-image", "sandbox-ai-update"]
TOKEN = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(scope="module")
def dist(tmp_path_factory):
    out = tmp_path_factory.mktemp("dist")
    subprocess.run([sys.executable, str(ROOT / "scripts/make_dist.py"), "--allow-dirty", "--skip-python",
                    "--out", str(out)], check=True, capture_output=True, env={**os.environ, "SOURCE_DATE_EPOCH": "0"})
    return out


class FakeGitea(http.server.ThreadingHTTPServer):
    """Releases API of one repository o/r, plus downloads and uploads; records Authorization headers.
    files is the release v<VERSION>; publish() adds others, the last one published is the newest."""
    def __init__(self, files):
        super().__init__(("127.0.0.1", 0), Handler)
        self.files, self.auth, self.uploads = dict(files), [], []
        self.tags, self.latest, self.rate_limited = {f"v{VERSION}": self.files}, f"v{VERSION}", False

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
        match = re.fullmatch(r"/o/r/releases/download/([^/]+)/(.+)", self.path)
        if match and match[2] in self.server.tags.get(match[1], {}):
            return self.reply(200, self.server.tags[match[1]][match[2]], "application/octet-stream")
        self.reply(404, {"message": "not found"})

    def do_POST(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/api/v1/repos/o/r/releases":
            return self.reply(201, {"id": 1, "tag_name": json.loads(body)["tag_name"], "assets": []})
        if self.path.startswith("/api/v1/repos/o/r/releases/1/assets"):
            self.server.uploads.append((self.path, self.headers.get("Content-Type"), body))
            return self.reply(201, {"id": len(self.server.uploads)})
        self.reply(404)

    def do_DELETE(self):
        self.server.auth.append((self.path, self.headers.get("Authorization")))
        self.server.uploads.append((self.path, "DELETE", b""))
        self.reply(204)


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
    version = subprocess.run([str(bins[0] / "claude-sandbox"), "--version"], capture_output=True, text=True)
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


def test_install_protects_foreign_files_and_refuses_bad_input(scenario, tmp_path, gitea):
    bin_dir = tmp_path / "home/.local/bin"
    bin_dir.mkdir(parents=True)
    if scenario == "happy":
        (bin_dir / "claude-sandbox").write_text("#!/bin/sh\necho mine\n")
        result = install(tmp_path, "--from", str(ROOT))
        assert result.returncode == 0 and "does not belong to sandbox-ai" in result.stderr
        assert (bin_dir / "claude-sandbox").read_text() == "#!/bin/sh\necho mine\n"
        forced = install(tmp_path, "--from", str(ROOT), "--force")
        assert forced.returncode == 0 and (bin_dir / "claude-sandbox").is_symlink()
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


def test_make_dist_is_reproducible(scenario, tmp_path, dist):
    other = tmp_path / "again"
    env = {**os.environ, "SOURCE_DATE_EPOCH": "0"}
    subprocess.run([sys.executable, str(ROOT / "scripts/make_dist.py"), "--allow-dirty", "--skip-python",
                    "--out", str(other)], check=True, capture_output=True, env=env)
    name = f"sandbox-ai-bundle-{VERSION}.tar.gz"
    assert (other / name).read_bytes() == (dist / name).read_bytes()
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
    args = [sys.executable, str(ROOT / "scripts/publish_release.py"), "--source", source, "--token-env", "RELEASE_TOKEN"]
    if scenario == "sad":
        result = subprocess.run([*args, *files], capture_output=True, text=True, env={**env, "RELEASE_TOKEN": ""})
        assert result.returncode != 0 and "RELEASE_TOKEN" in result.stderr
        result = subprocess.run([*args[:3], "http://git.example.invalid/o/r", "--token-env", "RELEASE_TOKEN", *files],
                                capture_output=True, text=True, env=env)
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
        assert "created release v9.9.9" in result.stderr


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
    assert rk.load() == []  # the repository ships no key until the maintainer adds one
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
            "RELEASE_TOKEN"]
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
    result = install(tmp_path, "--from", str(ROOT), PATH=f"{fake}:/usr/bin:/bin")
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
    code = "\n".join(line for line in code.splitlines() if not line.startswith('{"') and "[sys.argv[1]](" not in line)
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
