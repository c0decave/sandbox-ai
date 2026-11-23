"""mcp add NAME --build: a catalog entry without a signed image yet (pending: reviewed, not published) built here from
its source at the catalog's exact commit, checked with its smoke spec under sandbox-ai's own run line and added
UNSIGNED - and a start that names such a server asks for it (user report 2026-11-21: "MCP-Server jadx: kein Katalog
von coding-101 hat ihn ... warum werden die nicht automatisch gebaut oder gefragt ob sie direkt gebaut werden
sollen???"). End to end through ./sandbox-ai with Podman faked (live_fakes.Host, its podman wrapped below for the
build, the built image's digest and the image run as an MCP server); the GitHub path with its downloads faked."""
import base64
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest

from live_fakes import Host, ROOT


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


m = load("sandbox_mcp_build_test", "mcp.py")
b = m.build_module()
DEBIAN = "docker.io/library/debian:trixie-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad"
DIGEST = "sha256:" + "a" * 64
COMMIT = "1" * 40


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


@pytest.fixture(scope="module")
def mcp_key(tmp_path_factory):
    directory = tmp_path_factory.mktemp("mcp-build-key")
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(directory / "key")],
                   check=True)
    kind, key = (directory / "key.pub").read_text().split()[:2]
    k = m.release_keys()
    (directory / "signers").write_text(k.line(kind, key, k.MCP) + "\n")
    return directory


@pytest.fixture
def trusted(mcp_key, monkeypatch):
    monkeypatch.setitem(m.release_keys().MCP, "signers", mcp_key / "signers")
    return mcp_key


def signature(data, key):
    k = m.release_keys()
    return k.sign(data, key / "key", k.MCP)


JADX = {"id": "mcp:jadx", "name": "jadx", "title": "jadx (Android/Java decompiler, headless)", "kind": "oci",
        "image": "ghcr.io/c0decave/sandbox-ai-mcp-jadx", "tag": "latest", "network": "none",
        "mounts": [{"target": "/work", "description": "the apps", "required": True}],
        "repository": "https://github.com/c0decave/sandbox-ai-mcp/tree/main/servers/jadx"}
FETCHER = {"id": "mcp:fetcher", "name": "fetcher", "title": "a fetcher", "kind": "oci",
           "image": "ghcr.io/c0decave/sandbox-ai-mcp-fetcher", "network": "full"}
TIME = {"id": "mcp:time", "name": "time", "title": "Time", "kind": "oci",
        "image": "ghcr.io/c0decave/sandbox-ai-mcp-time", "digest": DIGEST, "network": "none"}


def catalog_bytes(*entries):
    return json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": list(entries)}).encode()


def fixtures_json():
    data = b"PK\x03\x04 a demo apk"
    return json.dumps({"schema": "sandbox-ai-mcp-fixtures", "version": 1, "files": [
        {"path": "demo.apk", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "what": "a demo",
         "gzip_base64": base64.b64encode(gzip.compress(data)).decode()}]})


def server_files(name="jadx", tools=("open_file", "list_files"), dockerfile=None, network="none"):
    """servers/NAME of a catalog repository: its Dockerfile (base image pinned), tools.json, smoke.json."""
    return {
        f"servers/{name}/Dockerfile": dockerfile or (f"ARG DEBIAN_IMAGE={DEBIAN}\n# the server\nFROM ${{DEBIAN_IMAGE}}"
                                                    " AS build\nRUN true\nFROM ${DEBIAN_IMAGE}\n"
                                                    "COPY --from=build /x /x\nCOPY src/server.py /opt/server.py\n"),
        f"servers/{name}/src/server.py": "print('a server')\n",
        f"servers/{name}/tools.json": json.dumps({"tools": list(tools)}),
        f"servers/{name}/server.json": json.dumps({"name": name}),
        f"servers/{name}/smoke.json": json.dumps({
            "name": name, "network": network, "memory": "1g", "pids": 256,
            "mounts": [{"host": "${FIXTURES}", "target": "/work"}], "expect_tools": [tools[0]],
            "calls": [{"tool": "list_files", "arguments": {}, "expect": "echo: list_files demo.apk"},
                      {"tool": "refuse", "arguments": {"path": "../../etc/passwd"}, "expect": "refused",
                       "expect_error": True}]}),
        "tests/fixtures/analysis-files.json": fixtures_json(),
    }


def git_catalog(folder, entries, files=None):
    """A catalog's own git checkout at FOLDER (catalog.json, servers/...), committed; its commit."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "catalog.json").write_bytes(catalog_bytes(*entries))
    for relative, text in (files if files is not None else server_files()).items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.org"}
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "catalog"]):
        subprocess.run(["git", "-C", str(folder), *argv], check=True, env=env, capture_output=True)
    return subprocess.run(["git", "-C", str(folder), "rev-parse", "HEAD"], check=True, capture_output=True,
                          text=True).stdout.strip()


def tarball(files, top="sandbox-ai-mcp-" + COMMIT, extra=()):
    """A GitHub-like archive (one top folder) of FILES; EXTRA: TarInfo members added as they are."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for relative, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"{top}/{relative}")
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for info, data in extra:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return buffer.getvalue()


# --- the GitHub path: the commit resolved, the catalog read at it, the archive by commit ---------------------------

MAIN = "https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/main/catalog.json"
AT = f"https://raw.githubusercontent.com/c0decave/sandbox-ai-mcp/{COMMIT}/catalog.json"


def github(trusted_key, entries=(JADX,), at_entries=None, sign=True):
    """(fetch, http, asked): the catalogs at main and at COMMIT (signed by TRUSTED_KEY), GitHub's API and archive."""
    main, at = catalog_bytes(*entries), catalog_bytes(*(at_entries or entries))
    files = {MAIN: main, AT: at}
    if sign:
        files.update({MAIN + ".sig": signature(main, trusted_key), AT + ".sig": signature(at, trusted_key)})
    asked = []

    def fetch(url):
        asked.append(url)
        if url in files:
            return files[url]
        raise OSError(f"{url}: not found")

    archive = tarball(server_files())

    def http(url, headers):
        asked.append(url)
        if url == "https://api.github.com/repos/c0decave/sandbox-ai-mcp/commits/main":
            assert headers["Accept"] == "application/vnd.github.sha"
            return COMMIT.encode()
        if url == f"https://codeload.github.com/c0decave/sandbox-ai-mcp/tar.gz/{COMMIT}":
            return archive
        raise OSError(f"{url}: not found")
    return fetch, http, asked, archive


def test_the_commit_is_resolved_once_and_the_entry_read_at_it(trusted):
    fetch, http, asked, archive = github(trusted)
    found = b.plan("jadx", [("mcp", MAIN)], fetch=fetch, http=http)
    assert found["commit"] == COMMIT and found["catalog"] == AT and found["entry"] == "mcp:jadx"
    assert found["repository"] == "https://github.com/c0decave/sandbox-ai-mcp" and found["path"] == "servers/jadx"
    assert found["signature"] == "good" and found["signer"] and found["image"] == "localhost/sandbox-ai-mcp-jadx"
    assert AT in asked and AT + ".sig" in asked  # the entry of that very commit, its signature checked
    data = b.archive_of(found, http=http, out=lambda text: None)
    assert data == archive and found["archive_sha256"] == hashlib.sha256(archive).hexdigest()
    # Kept by its commit: the build after the question takes the very bytes the question named (no second download).
    assert b.archive_of(dict(found), http=lambda url, headers: pytest.fail("downloaded again"),
                        out=lambda text: None) == archive
    text = b.question({**found})
    assert COMMIT in text and found["archive_sha256"] in text and "UNSIGNED" in text and "not signed" in text


def test_what_is_not_built(trusted):
    fetch, http, _asked, _archive = github(trusted)
    with pytest.raises(ValueError, match="at the commit 111111111111 now, not at 222222222222"):
        b.plan("jadx", [("mcp", MAIN)], fetch=fetch, http=http, commit="2" * 40)
    # An entry with a published image: `mcp add` pulls the signed one - --build never replaces it.
    fetch, http, _asked, _archive = github(trusted, entries=(TIME,))
    with pytest.raises(ValueError, match="has a published image in its catalog: sandbox-ai mcp add time"):
        b.plan("time", [("mcp", MAIN)], fetch=fetch, http=http)
    # The catalog published an image meanwhile (at the commit): refused all the same.
    fetch, http, _asked, _archive = github(trusted, at_entries=({**JADX, "digest": DIGEST},))
    with pytest.raises(ValueError, match="published image"):
        b.plan("jadx", [("mcp", MAIN)], fetch=fetch, http=http)
    # Not signed: --unsigned as for `mcp add`; a signature that does not verify: never.
    fetch, http, _asked, _archive = github(trusted, sign=False)
    with pytest.raises(ValueError, match="not signed by a trusted key"):
        b.plan("jadx", [("mcp", MAIN)], fetch=fetch, http=http)
    assert b.plan("jadx", [("mcp", MAIN)], fetch=fetch, http=http, unsigned=True)["signature"] == "unsigned"
    fetch, http, _asked, _archive = github(trusted)
    broken = lambda url: b"not a signature" if url.endswith(".sig") else fetch(url)  # noqa: E731
    for unsigned in (False, True):
        with pytest.raises(ValueError, match="signature does not verify"):
            b.plan("jadx", [("mcp", MAIN)], fetch=broken, http=http, unsigned=unsigned)
    # A catalog that names no source at an exact commit.
    with pytest.raises(ValueError, match="no source at an exact commit"):
        b.source_of("https://example.org/catalog.json", "jadx")


def test_the_archive_gives_only_the_server_folder(tmp_path):
    found = {"name": "jadx", "path": "servers/jadx", "commit": COMMIT}
    taken = b.extract(tarball({**server_files(), "scripts/smoke_test.py": "import os"}), found, tmp_path / "a")
    assert sorted(p.name for p in taken["server"].iterdir()) == ["Dockerfile", "server.json", "smoke.json", "src",
                                                                 "tools.json"]
    assert taken["fixtures"].is_file() and not (tmp_path / "a" / "scripts").exists()
    link = tarfile.TarInfo(f"sandbox-ai-mcp-{COMMIT}/servers/jadx/escape")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc"
    with pytest.raises(ValueError, match="no regular file or folder"):
        b.extract(tarball(server_files(), extra=[(link, None)]), found, tmp_path / "b")
    outside = tarfile.TarInfo(f"sandbox-ai-mcp-{COMMIT}/servers/jadx/../../x")
    outside.size = 1
    with pytest.raises(ValueError, match="a path outside it"):
        b.extract(tarball(server_files(), extra=[(outside, b"x")]), found, tmp_path / "c")
    with pytest.raises(ValueError, match="has no servers/other"):
        b.extract(tarball(server_files()), {**found, "path": "servers/other", "name": "other"}, tmp_path / "d")


def test_every_base_image_is_pinned_by_digest():
    assert b.base_images(server_files()["servers/jadx/Dockerfile"]) == [DEBIAN]
    for text, why in (("FROM docker.io/library/debian:trixie-slim\n", "not pinned by digest"),
                      ("ARG BASE\nFROM ${BASE}\n", "has no default"),
                      (f"FROM {DEBIAN} AS a\nCOPY --from=docker.io/x/y:1 /a /a\n", "COPY --from docker.io/x/y:1"),
                      (f"FROM {DEBIAN}\nRUN --mount=type=bind,from=ghcr.io/x/y:2,target=/s true\n", "RUN --mount"),
                      ("RUN true\n", "no FROM")):
        with pytest.raises(ValueError, match=why):
            b.base_images(text)
    assert b.base_images(f"FROM {DEBIAN} AS one\nFROM one\nFROM scratch\nCOPY --from=0 /a /a\n") == [DEBIAN]


# --- end to end: ./sandbox-ai with Podman faked ---------------------------------------------------------------------

# Wraps live_fakes' podman: `build` of a localhost/sandbox-ai-mcp-* image records it with a digest and the tools its
# folder records (FAKE_MCP_BUILD_FAIL=1: it fails, its output said; FAKE_MCP_EXTRA_TOOL=NAME: the image offers one
# more), `image inspect` / `image exists` / `image rm` know it, and `run ... IMAGE` of one runs it as an MCP server
# (its tools, list_files of the folder mounted at /work, refuse refused). Everything else: live_fakes' podman.
WRAPPER = r'''#!PYTHON
import hashlib, json, os, sys
from pathlib import Path
args = sys.argv[1:]
state = Path(os.environ["FAKE_MCP_BUILD_STATE"])
known = json.loads(state.read_text()) if state.exists() else {}
def save():
    state.write_text(json.dumps(known))
def option(name):
    return next((args[i + 1] for i, a in enumerate(args) if a == name and i + 1 < len(args)), None)
ours = [a for a in args if a.startswith("localhost/sandbox-ai-mcp-")]
if args[:1] == ["build"] and ours:
    with open(os.environ["FAKE_MCP_BUILD_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    print("STEP 1/3: FROM docker.io/library/debian:trixie-slim@sha256:62bd...", flush=True)
    if os.environ.get("FAKE_MCP_BUILD_FAIL"):
        print("STEP 2/3: RUN apt-get update", flush=True)
        print("E: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease (fake)", flush=True)
        print("Error: building at STEP \"RUN apt-get update\": exit status 100", flush=True)
        sys.exit(1)
    tag = option("--tag")
    tools = json.loads((Path(args[-1]) / "tools.json").read_text())["tools"]
    extra = os.environ.get("FAKE_MCP_EXTRA_TOOL")
    known[tag] = {"digest": "sha256:" + hashlib.sha256(tag.encode()).hexdigest(), "tools": tools + ([extra] if extra else [])}
    save()
    print("COMMIT " + tag, flush=True)
    sys.exit(0)
def find(ref):
    for tag, image in known.items():
        repo = tag.rsplit(":", 1)[0]
        if ref in (tag, repo + "@" + image["digest"]):
            return tag, image
    return None, None
if args[:2] == ["image", "inspect"] and ours:
    tag, image = find(args[-1])
    if image is None:
        sys.exit(125)
    print(json.dumps([tag.rsplit(":", 1)[0] + "@" + image["digest"]]) + " " + image["digest"])
    sys.exit(0)
if args[:2] == ["image", "exists"] and ours:
    sys.exit(0 if find(args[-1])[1] is not None else 1)
if args[:2] == ["image", "rm"] and ours:
    tag, image = find(args[-1])
    known.pop(tag, None)
    save()
    with open(os.environ["FAKE_MCP_BUILD_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    sys.exit(0)
if args[:1] == ["run"] and ours:
    with open(os.environ["FAKE_MCP_BUILD_LOG"], "a") as log:
        log.write(json.dumps(args) + "\n")
    tag, image = find(args[-1])
    if image is None:
        print("Error: image not known", file=sys.stderr)
        sys.exit(125)
    work = next((a.split("=", 1)[1].split(":")[0] for a in args if a.startswith("--volume=") and ":/work:" in a), None)
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method, ident = message["method"], message["id"]
        if method == "initialize":
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in image["tools"]]}
        elif message["params"]["name"] == "refuse":
            result = {"content": [{"type": "text", "text": "refused: a path outside /work"}], "isError": True}
        else:
            listed = " ".join(sorted(os.listdir(work))) if work else ""
            result = {"content": [{"type": "text", "text": "echo: " + message["params"]["name"] + " " + listed}]}
        print(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}), flush=True)
    sys.exit(0)
os.execv(os.environ["FAKE_MCP_BASE_PODMAN"], [os.environ["FAKE_MCP_BASE_PODMAN"], *sys.argv[1:]])
'''


class BuildHost(Host):
    """live_fakes.Host whose podman builds, inspects and runs the images mcp add --build makes (WRAPPER)."""

    def __init__(self, directory, user="user"):
        super().__init__(directory, user)
        (self.bin / "podman").rename(self.bin / "podman-base")
        (self.bin / "podman").write_text(WRAPPER.replace("PYTHON", sys.executable, 1))
        (self.bin / "podman").chmod(0o755)
        self.build_log = self.directory / "mcp-build.log"
        self.env.update(FAKE_MCP_BUILD_STATE=str(self.directory / "mcp-build-images.json"),
                        FAKE_MCP_BUILD_LOG=str(self.build_log), FAKE_MCP_BASE_PODMAN=str(self.bin / "podman-base"))
        self.catalogs = self.directory / "catalogs"
        self.apps = self.directory / "apps"
        self.apps.mkdir()

    def build_calls(self):
        return [json.loads(line) for line in self.build_log.read_text().splitlines()] if self.build_log.exists() \
            else []

    def definitions(self):
        path = self.home / ".config/sandbox-ai/mcp.json"
        return json.loads(path.read_text()) if path.exists() else {}


@pytest.fixture
def host(tmp_path):
    made = BuildHost(tmp_path / "h")
    made.commit = git_catalog(made.catalogs / "sandbox-ai-mcp", [JADX, FETCHER, TIME],
                              {**server_files(), **server_files("fetcher", ("fetch",), network="full")})
    (made.catalogs / "axross-mcp-hub").mkdir()
    (made.catalogs / "axross-mcp-hub" / "catalog.json").write_bytes(catalog_bytes())
    made.catalog = str(made.catalogs / "sandbox-ai-mcp" / "catalog.json")
    made.env["SANDBOX_AI_LOCAL_CATALOGS"] = str(made.catalogs)  # (a start reads these: no network)
    return made


def plan_of(host, *extra):
    result = host.run("mcp", "add", "jadx", "--build-plan", "--unsigned", "--catalog", host.catalog, *extra)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_build_plan_then_build_adds_it_unsigned(host):
    """The app's and the web interface's way: the plan (what, which commit, the archive's hash: nothing built), the
    human's yes there, then exactly that built (--commit, --source-sha256, --yes)."""
    plan = plan_of(host, "--mount", f"/work={host.apps}")
    assert plan["commit"] == host.commit and plan["entry"] == "c0:jadx" and plan["kind"] == "git"
    assert plan["signature"] == "unsigned" and len(plan["archive_sha256"]) == 64 and host.build_calls() == []
    assert host.commit in plan["question"] and "UNSIGNED" in plan["question"]
    result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--mount",
                      f"/work={host.apps}", "--yes", "--commit", plan["commit"], "--source-sha256",
                      plan["archive_sha256"])
    assert result.returncode == 0, result.stderr
    build, *runs = host.build_calls()
    assert build[:3] == ["build", "--platform=linux/amd64", "--pull=missing"]
    assert f"sandbox-ai.mcp.commit={host.commit}" in build and "sandbox-ai.mcp.signed=no" in build
    assert build[build.index("--tag") + 1] == f"localhost/sandbox-ai-mcp-jadx:build-{host.commit[:12]}"
    # The smoke run: sandbox-ai's own run line (no capabilities, read-only, no network, the spec's limits), the
    # fixtures at /work - list_files saw demo.apk there, the refused call was refused.
    (smoke,) = [call for call in runs if call[0] == "run"]
    for flag in ("--cap-drop=ALL", "--read-only", "--network=none", "--memory=1g", "--pids-limit=256",
                 "--security-opt=no-new-privileges", "--pull=never"):
        assert flag in smoke, flag
    said = result.stdout + result.stderr
    assert "smoke: list_files: ok" in said and "smoke: refuse: ok" in said
    definition = host.definitions()["jadx"]
    assert definition["signed_by"] is None and definition["built"]["commit"] == host.commit
    assert definition["built"]["archive_sha256"] == plan["archive_sha256"]
    assert definition["image"].startswith("localhost/sandbox-ai-mcp-jadx@sha256:")
    assert definition["mounts"] == [{"target": "/work", "host": str(host.apps), "mode": "ro"}]
    assert "built here from" in said and "built here, not signed" in said
    listed = host.run("mcp", "list")
    assert "built here, not signed" in listed.stdout and ", signed" not in listed.stdout
    (row,) = [row for row in json.loads(host.run("mcp", "list", "--json").stdout) if row["name"] == "jadx"]
    assert row["signed"] is False and row["built"]["commit"] == host.commit


def test_nothing_is_built_without_a_yes(host):
    result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--mount",
                      f"/work={host.apps}")
    assert result.returncode == 1 and "nothing is built without a yes" in result.stderr
    assert host.build_calls() == [] and "jadx" not in host.definitions()
    # What was asked about, and nothing else: another commit or archive is refused before anything is built.
    for option, value, why in (("--commit", "2" * 40, "not at 222222222222"),
                               ("--source-sha256", "0" * 64, "not\n?\\s*0{64}")):
        result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--mount",
                          f"/work={host.apps}", "--yes", option, value)
        assert result.returncode == 1 and "nothing is built" in result.stderr, result.stderr
    assert host.build_calls() == []
    # A folder that is not there is said before anything is downloaded or built (none given: its default, 0.1.212).
    result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--yes", "--mount",
                      f"/work={host.apps}/not-there")
    assert result.returncode == 1 and "a read-only folder must be there" in result.stderr and host.build_calls() == []


def test_a_signed_image_is_never_built_over(host):
    result = host.run("mcp", "add", "time", "--build", "--unsigned", "--catalog", host.catalog, "--yes")
    assert result.returncode == 1 and "has a published image in its catalog: sandbox-ai mcp add time" in result.stderr
    assert host.build_calls() == []


def test_a_failed_build_is_shown_with_its_log(host):
    plan = plan_of(host, "--mount", f"/work={host.apps}")
    result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--mount",
                      f"/work={host.apps}", "--yes", "--commit", plan["commit"], env={"FAKE_MCP_BUILD_FAIL": "1"})
    assert result.returncode == 1
    assert "its build failed (exit status 1)" in result.stderr and "E: Failed to fetch" in result.stderr
    assert "  | E: Failed to fetch" in result.stdout  # (as it went: the app's tab shows it)
    last = result.stderr.strip().splitlines()[-1]  # (what failed and where its log is: the line the app shows)
    assert last.startswith("sandbox-ai mcp: jadx is not added: its build failed (exit status 1); the whole log: ")
    log = Path(last.split("the whole log: ")[1])
    assert "E: Failed to fetch" in log.read_text() and log.stat().st_mode & 0o077 == 0
    assert "jadx" not in host.definitions()


def test_an_image_that_fails_its_smoke_test_is_not_added(host):
    result = host.run("mcp", "add", "jadx", "--build", "--unsigned", "--catalog", host.catalog, "--mount",
                      f"/work={host.apps}", "--yes", env={"FAKE_MCP_EXTRA_TOOL": "sneaky"})
    assert result.returncode == 1, result.stderr
    assert "does not pass its smoke test" in result.stderr and "not recorded: sneaky" in result.stderr
    assert "jadx" not in host.definitions()
    assert any(call[:2] == ["image", "rm"] for call in host.build_calls())  # (the image built is removed again)


def test_only_what_is_committed_is_built(host):
    (host.catalogs / "sandbox-ai-mcp" / "servers/jadx/src/server.py").write_text("print('changed')\n")
    result = host.run("mcp", "add", "jadx", "--build-plan", "--unsigned", "--catalog", host.catalog, "--mount",
                      f"/work={host.apps}")
    assert result.returncode == 1 and "only what is committed is built" in result.stderr


# --- a start that names such a server ------------------------------------------------------------------------------

QUESTION = b"[Y]es, all / [c]hoose / [n]o:"  # the start's one question about everything it lacks (sandbox/fixes.py)


def on_terminal(host, *args, answer="y", timeout=120, env=None):
    """`sandbox-ai ARGS` on a pseudo-terminal, ANSWER given to the start's question about what it lacks: (exit
    status, what it said)."""
    controller, terminal = pty.openpty()
    process = subprocess.Popen(host.command(*args), cwd=host.workspace, env={**host.env, **(env or {})},
                               stdin=terminal, stdout=terminal, stderr=terminal, close_fds=True)
    os.close(terminal)
    said, answered, deadline = b"", False, time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([controller], [], [], 0.2)
        if ready:
            try:
                more = os.read(controller, 65536)
            except OSError:
                break
            if not more:
                break
            said += more
            if not answered and QUESTION in said:
                os.write(controller, answer.encode() + b"\n")
                answered = True
        elif process.poll() is not None:
            break
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    os.close(controller)
    return process.returncode, " ".join(said.decode(errors="replace").split())


START = ("codex", "--backend", "podman", "--mcp", "jadx")


def test_a_start_without_a_terminal_names_the_build_command(host):
    """The user's report: the start did not go on, and said only to define the server. Now it names the command that
    builds it (with the folder the start gives it); a server in no catalog keeps today's words."""
    host.add_image("localhost/codex-sandbox:latest")
    result = host.run(*START, "--mcp-mount", f"jadx:/work={host.apps}")
    text = " ".join(result.stderr.split())
    assert result.returncode != 0 and "Unknown MCP server: jadx" in text
    assert f"`sandbox-ai mcp add jadx --build --unsigned --mount /work={host.apps}` builds it here" in text
    assert host.build_calls() == [] and host.stopped_at() is None
    result = host.run("codex", "--backend", "podman", "--mcp", "nowhere")
    assert "Unknown MCP server: nowhere" in result.stderr and "--build" not in result.stderr


def test_a_start_on_a_terminal_asks_builds_and_goes_on(host):
    host.add_image("localhost/codex-sandbox:latest")
    status, said = on_terminal(host, *START, "--mcp-mount", f"jadx:/work={host.apps}", "--dry-run", answer="y")
    # A dry run (the app's and the Board's checks, often) neither asks nor reads the catalogs for it: refused as before.
    asked = QUESTION.decode()
    assert asked not in said and "Unknown MCP server: jadx" in said and host.build_calls() == []
    # The start's one question about everything it lacks (sandbox/fixes.py; before 0.1.208 a question of its own,
    # "build it? [y/N]"): the server named as built here from its source, UNSIGNED.
    status, said = on_terminal(host, *START, "--mcp-mount", f"jadx:/work={host.apps}", answer="n")
    assert asked in said and "UNSIGNED" in said and "build it here from its reviewed source" in said
    assert status != 0 and "Unknown MCP server: jadx" in said and host.build_calls() == []
    status, said = on_terminal(host, *START, "--mcp-mount", f"jadx:/work={host.apps}", answer="y")
    assert host.commit[:12] in said, "the commit that is built is said as it is built"
    assert "smoke: list_files: ok" in said and "jadx" in host.definitions(), said
    assert host.definitions()["jadx"]["signed_by"] is None
    assert "Unknown MCP server" not in said and host.stopped_at() is not None, said  # (the start went on: its pod)
    assert host.run("mcp", "stop", "jadx").returncode == 0  # (the server the start started: ended)


@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_the_security_modes_take_it_as_any_unsigned_server(host, mode):
    """An offer never widens the security mode (the user's decision, 2026-11-22): in high a server that would be
    built here - unsigned - is not offered, and the refusal names the laxer mode that takes it; paranoid refuses a
    start with it too - judged by its catalog's entry (journeys round 1: a server not added yet is no longer taken
    for one with network): fetcher's network refused, jadx's unsigned build not offered - nothing asked or built."""
    host.add_image("localhost/codex-sandbox:latest")
    for server in ("fetcher", "jadx"):
        status, said = on_terminal(host, "codex", "--backend", "podman", "--mode", mode, "--mcp", server,
                                   "--mcp-mount", f"jadx:/work={host.apps}:ro", answer="n")
        if mode == "paranoid":
            assert "build it? [y/N]" not in said and status != 0, said
            assert ("the security mode paranoid refuses it (the MCP server reaches the network" if server == "fetcher"
                    else "not offered in the security mode paranoid") in " ".join(said.split()), said
        else:
            assert QUESTION.decode() not in said and status != 0, said
            assert "not offered in the security mode high" in said and "--mode medium" in said, said
    assert host.build_calls() == []


def test_the_offer_in_process(monkeypatch, tmp_path):
    """mcp_build_offers with the question given: declined -> the hint; a build that fails -> its words; built -> added."""
    s = load("agent_sandbox_mcp_build_test", "agent_sandbox.py")
    mcp = s.load_mcp_module()
    entry = {**JADX, "_catalog": "/x/catalog.json", "_signature": {"state": "good", "signer": "SHA256:k"}}
    monkeypatch.setattr(mcp, "catalog_entries", lambda sources=None, fetch=None: ([("mcp", entry)], []))
    calls = []

    def build_and_add(name, options, **kwargs):
        calls.append((name, options, kwargs["unsigned"]))
        if not kwargs["confirm"]("build jadx at 1111?"):
            raise ValueError("jadx is not built: not confirmed.")
        if len(calls) == 2:
            raise mcp.build_module().BuildFailed("jadx is not added: its build failed (exit status 1)")
        return {}
    monkeypatch.setattr(mcp.build_module(), "build_and_add", build_and_add)
    a = SimpleNamespace(dry_run=False, podman_url=None, mcp_mount=[f"jadx:/work={tmp_path}"], mode="medium",
                        project_document=None)
    monkeypatch.setattr(s.MODES, "widening", lambda l, a, mode: [])
    assert s.mcp_mode_refusal(a, "jadx", {"image": s.MCP_BUILD_PLACEHOLDER, "network": "full"}) is None
    assert s.mcp_build_offers(a, mcp, ["jadx"], ask=lambda text: False)[1][0].startswith("jadx: its catalog")
    added, hints = s.mcp_build_offers(a, mcp, ["jadx"], ask=lambda text: True)
    assert added == [] and "its build failed" in hints[0]
    assert s.mcp_build_offers(a, mcp, ["jadx"], ask=lambda text: True) == (["jadx"], [])
    assert calls[0] == ("mcp:jadx", {"mounts": [f"/work={tmp_path}"]}, False)  # (a signed catalog: no --unsigned)
    # Where the mode would refuse the server built (its network, in paranoid), it is not offered: the mode's words.
    monkeypatch.setattr(s.MODES, "widening", lambda l, a, mode: [
        (f"--mcp {n}", "it reaches the network") for n in l.load_mcp_module().definitions() if n == "jadx"])
    added, hints = s.mcp_build_offers(a, mcp, ["jadx"], ask=lambda text: pytest.fail("asked"))
    assert added == [] and "the security mode medium refuses it (it reaches the network)" in hints[0]


def test_a_server_built_here_goes_to_another_host_as_an_image_of_your_own(monkeypatch, tmp_path):
    """mcp export / copy: its catalog has no image of it to add there, so it goes as an image of your own (its image
    copied, --copy-image), never as a signed catalog server - and nothing there calls it built there."""
    image = "localhost/sandbox-ai-mcp-jadx@sha256:" + "b" * 64
    record = {"commit": COMMIT, "repository": "https://github.com/c0decave/sandbox-ai-mcp", "path": "servers/jadx",
              "archive_sha256": "c" * 64, "at": "2026-11-21T08:00:00Z"}
    m.write_definition("jadx", {"image": image, "network": "none", "signed_by": None, "source": "mcp:jadx",
                                "catalog": AT, "built": record})
    copier = m.copy_module()
    document = copier.export("jadx", run=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr=""))
    assert document["kind"] == "image" and "catalog" not in document
    with pytest.raises(ValueError, match="never signed"):
        m.validated_image({"image": image, "built": record, "signed_by": "SHA256:x"}, "jadx")
    with pytest.raises(ValueError, match="built is"):
        m.validated_image({"image": image, "built": {**record, "commit": "main"}}, "jadx")
    assert m.origin("jadx", m.definitions()["jadx"])["signed"] is False


def test_on_another_host_through_hosts_run(tmp_path):
    """The app's way for a start on another host, from the command line: `sandbox-ai hosts run HOST -- mcp add NAME
    --build-plan`, then the build of exactly that there - built by that host's Podman, added there, nothing here."""
    here, there = BuildHost(tmp_path / "here"), BuildHost(tmp_path / "there")
    commit = git_catalog(there.catalogs / "sandbox-ai-mcp", [JADX])
    catalog = str(there.catalogs / "sandbox-ai-mcp" / "catalog.json")
    here.route_ssh("user@192.0.2.9", there)
    assert here.run("hosts", "add", "ct1", "user@192.0.2.9").returncode == 0
    words = ["mcp", "add", "jadx", "--unsigned", "--catalog", catalog, "--mount", f"/work={there.apps}"]
    planned = here.run("hosts", "run", "ct1", "--", *words, "--build-plan", timeout=300)
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["commit"] == commit
    built = here.run("hosts", "run", "ct1", "--", *words, "--build", "--yes", "--commit", plan["commit"],
                     "--source-sha256", plan["archive_sha256"], timeout=300)
    assert built.returncode == 0, built.stderr
    assert there.definitions()["jadx"]["built"]["commit"] == commit and there.build_calls()
    assert here.build_calls() == [] and "jadx" not in here.definitions()


def test_a_smoke_spec_never_sets_what_this_hosts_programs_read():
    for name in ("LD_PRELOAD", "CONTAINERS_CONF", "HTTPS_PROXY", "https_proxy", "PATH", "PYTHONPATH", "XDG_CONFIG_HOME",
                 "NODE_OPTIONS"):
        assert b.host_variable(name), name
    assert b.host_variable("LOCAL_TIMEZONE") is None and b.host_variable("BRAVE_API_KEY") is None
