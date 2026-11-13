"""Your images (sandbox-ai images pull|build-image|list|remove) and services (--service), with podman replaced by
recorded answers: references and digests, the record, Containerfiles pinned by digest, the services' syntax and the
launcher's checks (backend, remote engine, Tor, the firewall's users and ports), their podman run line, the
handshake with the supervisor, the dry run, a variant on a base image of yours and the security modes."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sv = load("services_tests", "sandbox/services.py")
im = load("images_for_services_tests", "sandbox/images.py")
s = load("launcher_for_services_tests", "sandbox/agent_sandbox.py")
sv._images = im
sv._launcher = s
im._services = sv
DIGEST = "sha256:" + "a" * 64
OTHER = "sha256:" + "c" * 64
ID = "b" * 64


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(im, "user_bus", lambda uid=None: True)
    return tmp_path


def tar_of(name, data, link=None):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        info = tarfile.TarInfo(name)
        if link:
            info.type, info.linkname = tarfile.SYMTYPE, link
            archive.addfile(info)
        else:
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class Podman:
    """Recorded answers of podman: an image store of {reference: inspect document}, files in images, and the calls."""

    def __init__(self, store=None, files=None, fail=()):
        self.store, self.files, self.fail, self.calls = dict(store or {}), dict(files or {}), set(fail), []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        words = argv[1:]
        verb = words[0] if words else ""
        if verb in self.fail:
            return subprocess.CompletedProcess(argv, 125, "", f"{verb} failed")
        if verb == "pull":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if words[:2] == ["image", "inspect"]:
            data = self.store.get(words[-1])
            return subprocess.CompletedProcess(argv, 0 if data else 125, json.dumps([data]) if data else "",
                                               "" if data else "image not known")
        if verb == "create":
            return subprocess.CompletedProcess(argv, 0, "container1\n", "")
        if verb == "cp":
            path = words[1].partition(":")[2]
            if path in self.files:
                value = self.files[path]
                data = tar_of(Path(path).name, b"", link=value[5:]) if value.startswith("LINK:") else \
                    tar_of(Path(path).name, value.encode())
                return subprocess.CompletedProcess(argv, 0, data, b"")
            return subprocess.CompletedProcess(argv, 1, b"", b"no such file")
        if verb in ("rm", "build", "tag"):
            if verb == "build":
                tag = words[words.index("--tag") + 1]
                self.store.setdefault(tag, {"Id": "sha256:" + "d" * 64, "Config": {}, "Os": "linux",
                                            "Architecture": "amd64"})
            return subprocess.CompletedProcess(argv, 0, "", "")
        if verb == "images":
            ids = sorted({str(d.get("Id", "")) for d in self.store.values()})
            return subprocess.CompletedProcess(argv, 0, "\n".join(ids) + "\n", "")
        if words[:2] == ["image", "rm"]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)


REDIS = {"Id": "sha256:" + ID, "Digest": DIGEST, "RepoDigests": [f"docker.io/library/redis@{DIGEST}"],
         "Created": "2026-10-01T10:00:00.5Z", "Size": 40_000_000, "Os": "linux", "Architecture": "amd64",
         "Config": {"User": "redis", "ExposedPorts": {"6379/tcp": {}}, "Env": ["PATH=/usr/bin"]}}
FILES = {"/etc/passwd": "root:x:0:0::/root:/bin/sh\nredis:x:999:1000::/data:/bin/sh\n",
         "/etc/os-release": 'ID=alpine\nVERSION_ID=3.20.3\nPRETTY_NAME="Alpine Linux v3.20"\n'}
BOOKWORM = {"/etc/passwd": "root:x:0:0::/root:/bin/bash\n", "/etc/os-release": "LINK:../usr/lib/os-release",
            "/usr/lib/os-release": 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\nID=debian\nVERSION_ID="12"\n'
                                   "VERSION_CODENAME=bookworm\n"}


def add_redis(podman=None):
    podman = podman or Podman({"docker.io/library/redis:7-alpine": REDIS,
                               f"docker.io/library/redis@{DIGEST}": REDIS}, FILES)
    lines = []
    name, entry = sv.pull("redis:7-alpine", podman=["podman"], run=podman, out=lines.append)
    return name, entry, lines, podman


# -- references and digests --

@pytest.mark.parametrize("ref, expected", [
    ("redis", ("docker.io", "library/redis", "latest", None)),
    ("redis:7-alpine", ("docker.io", "library/redis", "7-alpine", None)),
    ("docker.io/searxng/searxng:2024.1", ("docker.io", "searxng/searxng", "2024.1", None)),
    ("index.docker.io/library/postgres", ("docker.io", "library/postgres", "latest", None)),
    (f"ghcr.io/org/app@{DIGEST}", ("ghcr.io", "org/app", None, DIGEST)),
    (f"quay.io/org/app:1.0@{DIGEST}", ("quay.io", "org/app", "1.0", DIGEST)),
    ("registry.example.org:5000/team/tool:v2", ("registry.example.org:5000", "team/tool", "v2", None)),
    ("localhost/mine:latest", ("localhost", "mine", "latest", None)),
])
def test_parse_reference(ref, expected):
    parsed = sv.parse_reference(ref)
    assert (parsed["domain"], parsed["path"], parsed["tag"], parsed["digest"]) == expected


@pytest.mark.parametrize("ref", ["", "Redis", "redis@sha256:abc", "redis:bad tag", "a b", "redis:", "../x",
                                 "ghcr.io/", "x" * 600, "redis\n"])
def test_parse_reference_refuses(ref):
    with pytest.raises(ValueError):
        sv.parse_reference(ref)


def test_repo_digest_prefers_the_images_own_manifest():
    data = {"Digest": OTHER, "RepoDigests": [f"docker.io/library/redis@{DIGEST}", f"docker.io/library/redis@{OTHER}"]}
    assert sv.repo_digest(data, "docker.io/library/redis") == OTHER
    assert sv.repo_digest({"RepoDigests": [f"redis@{DIGEST}"]}, "docker.io/library/redis") == DIGEST
    assert sv.repo_digest({"RepoDigests": ["x@nonsense"]}, "docker.io/library/x") is None


def tar_members(*members):
    """A tar stream of MEMBERS: (name, type, data)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, kind, data in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return buffer.getvalue()


def copied(stream):
    """A fake podman for image_file whose cp answers STREAM."""
    def run(argv, **kwargs):
        if argv[1] == "create":
            return subprocess.CompletedProcess(argv, 0, "container1\n", "")
        if argv[1] == "cp":
            return subprocess.CompletedProcess(argv, 0, stream, b"")
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


@pytest.mark.parametrize("stream", [
    tar_members(("passwd", tarfile.DIRTYPE, b""), ("passwd/x", tarfile.REGTYPE, b"root:x:0:0::/:/bin/sh\n")),
    tar_members(("passwd", tarfile.FIFOTYPE, b"")),
    tar_members(("passwd", tarfile.CHRTYPE, b"")),
    tar_members(("passwd", tarfile.LNKTYPE, b"")),
    tar_members(("passwd", tarfile.REGTYPE, b"a" * ((1 << 20) + 1))),           # over the limit
    tar_members(("passwd", tarfile.REGTYPE, b"x\n"), ("other", tarfile.REGTYPE, b"y\n")),  # more than the file
], ids=["directory", "fifo", "device", "hardlink", "too-large", "two-members"])
def test_image_file_reads_only_one_small_regular_file(stream):
    """/etc/passwd and /etc/os-release of an image nobody reviewed: a directory, a special file, a link to another
    member, more than the file or more than LIMIT is no answer (None) - never read into memory whole."""
    assert sv.image_file("img", "/etc/passwd", ["podman"], copied(stream)) is None
    assert sv.image_file("img", "/etc/passwd", ["podman"],
                         copied(tar_members(("passwd", tarfile.REGTYPE, b"root:x:0:0::/:/bin/sh\n")))) == \
        "root:x:0:0::/:/bin/sh\n"


def test_image_file_stops_reading_a_stream_past_its_bound(monkeypatch):
    """The copy from the container is read as a stream and stopped (killed) once it holds more than the file's limit
    and a tar header could: a /etc/passwd of gigabytes never reaches this process's memory."""
    class Endless:
        def __init__(self, argv, **kwargs):
            self.argv, self.read_bytes, self.killed, self.returncode = argv, 0, False, None
            outer = self

            class Out:
                def read(self, size=-1):
                    assert size and size > 0, "read in chunks"
                    outer.read_bytes += size
                    return b"\0" * size
            self.stdout = Out()

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            self.returncode = -9
            return -9

        def poll(self):
            return self.returncode
    made = []

    def popen(argv, **kwargs):
        made.append(Endless(argv, **kwargs))
        return made[-1]
    monkeypatch.setattr(sv.subprocess, "Popen", popen)
    calls = []

    def run(argv, **kwargs):  # create and rm through run; the copy streams through Popen
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "container1\n" if argv[1] == "create" else "", "")
    assert sv.image_file("img", "/etc/passwd", ["podman"], run, copy=None) is None
    assert made and made[0].argv[1] == "cp" and made[0].killed
    assert made[0].read_bytes <= (1 << 20) + (1 << 17), made[0].read_bytes
    assert ["podman", "rm", "--force", "container1"] in calls, "the container goes in any case"


def test_numeric_user_and_os_release():
    passwd = FILES["/etc/passwd"]
    assert sv.numeric_user("", passwd) == "0:0" and sv.numeric_user("root", passwd) == "0:0"
    assert sv.numeric_user("redis", passwd) == "999:1000"
    assert sv.numeric_user("redis:5", passwd) == "999:5"
    assert sv.numeric_user("1001", passwd) == "1001:1001"
    assert sv.numeric_user("nobody", passwd) is None  # unknown names are never guessed
    assert sv.numeric_user("redis:staff", passwd) is None
    assert sv.os_release('ID=debian\nVERSION_CODENAME=bookworm\nX=1\nbad line\nPRETTY_NAME="Debian 12"') == \
        {"ID": "debian", "VERSION_CODENAME": "bookworm", "PRETTY_NAME": "Debian 12"}


# -- the record --

def test_pull_pins_by_digest_and_warns(config):
    name, entry, lines, podman = add_redis()
    assert name == "redis" and entry["image"] == f"docker.io/library/redis@{DIGEST}" and entry["digest"] == DIGEST
    assert entry["user"] == "999:1000" and entry["ports"] == [6379] and entry["os"]["ID"] == "alpine"
    assert entry["reviewed"] is False and entry["id"] == "sha256:" + ID
    assert ["podman", "pull", "docker.io/library/redis:7-alpine"] in podman.calls
    assert any("UNCHECKED" in line for line in lines)
    # nothing of the image ran: a container was created (never started) to copy its files
    assert not any(call[1] in ("run", "start") for call in podman.calls)
    assert all("--network=none" in call for call in podman.calls if call[1] == "create")
    saved = json.loads((config / "config" / "sandbox-ai" / "user-images.json").read_text())
    assert saved["version"] == 1 and saved["images"]["redis"]["image"] == entry["image"]
    assert oct((config / "config" / "sandbox-ai" / "user-images.json").stat().st_mode & 0o777) == "0o600"
    assert [h["action"] for h in im.history("redis")] == ["pull"]


def test_pull_by_digest_keeps_it(config):
    podman = Podman({f"ghcr.io/org/app@{DIGEST}": {**REDIS, "RepoDigests": []}}, FILES)
    name, entry = sv.pull(f"ghcr.io/org/app@{DIGEST}", "app", podman=["podman"], run=podman, out=lambda _: None)
    assert entry["image"] == f"ghcr.io/org/app@{DIGEST}" and ["podman", "pull", f"ghcr.io/org/app@{DIGEST}"] in \
        podman.calls


def test_pull_refuses(config):
    add_redis()
    with pytest.raises(ValueError, match="--replace"):
        add_redis()
    newer = {**REDIS, "Id": "sha256:" + "e" * 64, "Digest": OTHER, "RepoDigests": [f"docker.io/library/redis@{OTHER}"]}
    podman = Podman({"docker.io/library/redis:7-alpine": newer}, FILES)
    _, entry = sv.pull("redis:7-alpine", replace=True, podman=["podman"], run=podman, out=lambda _: None)
    assert entry["digest"] == OTHER and im.history("redis")[-1]["replaced"] == f"docker.io/library/redis@{DIGEST}"
    with pytest.raises(ValueError, match="a local image"):
        sv.pull("localhost/mine", podman=["podman"], run=podman, out=lambda _: None)
    with pytest.raises(ValueError, match="pull .* failed"):
        sv.pull("redis:7", "r7", podman=["podman"], run=Podman(fail={"pull"}), out=lambda _: None)
    with pytest.raises(ValueError, match="no digest"):
        sv.pull("redis:8", "r8", podman=["podman"],
                run=Podman({"docker.io/library/redis:8": {**REDIS, "Digest": "", "RepoDigests": []}}, FILES),
                out=lambda _: None)
    im.new_variant("web", "claude", empty=True)
    with pytest.raises(ValueError, match="name of an image variant"):
        sv.pull("redis:7-alpine", "web", podman=["podman"], run=podman, out=lambda _: None)
    with pytest.raises(ValueError, match="a name is"):
        sv.pull("redis:7-alpine", "claude", podman=["podman"], run=podman, out=lambda _: None)


def test_damaged_records_are_refused(config):
    path = config / "config" / "sandbox-ai" / "user-images.json"
    path.parent.mkdir(parents=True)
    for document in ({"version": 2, "images": {}}, {"version": 1, "images": {"x": {"kind": "pulled", "id": ID,
                                                                                    "image": "docker.io/x:latest"}}},
                     {"version": 1, "images": {"x": {"kind": "built", "id": ID, "image": "docker.io/x@" + DIGEST}}},
                     {"version": 1, "images": {"Bad": {"kind": "pulled"}}}):
        path.write_text(json.dumps(document))
        with pytest.raises(ValueError):
            sv.read_registry()
    path.write_text("{")
    with pytest.raises(ValueError):
        sv.read_registry()


def test_listing_and_remove(config, capsys):
    add_redis()
    podman = Podman({"x": REDIS})
    rows = sv.listing(["podman"], podman)
    assert rows[0]["name"] == "redis" and rows[0]["present"] is True and rows[0]["unchecked"] is True
    assert "Alpine" in rows[0]["base_problem"] or "Debian 12" in rows[0]["base_problem"]
    lines = []
    sv.show_rows(rows, lines.append)
    assert "UNCHECKED" in lines[0] and "user 999:1000" in lines[1]
    assert sv.listing(["podman"], Podman({}))[0]["present"] is False
    assert sv.remove("redis", podman=["podman"], run=podman) == []
    assert ["podman", "image", "rm", f"docker.io/library/redis@{DIGEST}"] in podman.calls
    assert sv.read_registry() == {}
    with pytest.raises(ValueError, match="no image redis of yours"):
        sv.remove("redis", podman=["podman"], run=podman)
    lines = []
    sv.show_rows([], lines.append)
    assert "images pull" in lines[0]


# -- your own Containerfiles --

def test_external_images_and_pins():
    text = ("# syntax comment\nFROM --platform=linux/amd64 golang:1.23 AS build\nRUN go build\n"
            "FROM build AS again\nFROM scratch\nCOPY --from=build /x /x\nCOPY --from=0 /y /y\n"
            "COPY --from=docker.io/library/busybox:1.36 /bin/busybox /bin/\nFROM python:3.12-slim\n")
    assert sv.external_images(text) == [(2, "golang:1.23"), (8, "docker.io/library/busybox:1.36"),
                                        (9, "python:3.12-slim")]
    pinned = sv.pin_containerfile(text, {"golang:1.23": "docker.io/library/golang@" + DIGEST,
                                         "docker.io/library/busybox:1.36": "docker.io/library/busybox@" + DIGEST,
                                         "python:3.12-slim": "docker.io/library/python@" + OTHER})
    assert "FROM --platform=linux/amd64 docker.io/library/golang@" + DIGEST + " AS build" in pinned
    assert "COPY --from=docker.io/library/busybox@" + DIGEST + " /bin/busybox /bin/" in pinned
    assert pinned.rstrip().endswith("FROM docker.io/library/python@" + OTHER) and "FROM build AS again" in pinned
    # a stage named later is no stage yet: FROM it is an image
    assert sv.external_images("FROM later\nFROM debian AS later\n") == [(1, "later"), (2, "debian")]
    for text, match in (("ARG V=1\nFROM debian:${V}\n", "variable"), ("FROM debian\nCOPY --from=$X / /\n", "variable"),
                        ("FROM debian\nRUN --mount=type=bind,from=alpine,target=/a ls\n", "RUN --mount"),
                        ("FROM --platform=x\n", "names no image")):
        with pytest.raises(ValueError, match=match):
            sv.external_images(text)
    # Another escape character (a parser directive): the engine then reads "RUN echo \" as complete and the next line
    # as an instruction of its own - an unpinned FROM that the backslash-joined lines here never saw.
    hidden = "# escape=`\nFROM debian:bookworm\nRUN echo \\\nFROM evil-local\n"
    for text in (hidden, "#ESCAPE = `\nFROM debian\n", "# syntax=docker/dockerfile:1\n# escape=`\nFROM debian\n"):
        with pytest.raises(ValueError, match="escape"):
            sv.external_images(text)
    # The backslash itself, or a comment that only looks like one after an instruction, changes nothing.
    assert sv.external_images("# escape=\\\nFROM debian\n") == [(2, "debian")]
    assert sv.external_images("FROM debian\n# escape=`\n") == [(1, "debian")]


def test_build_pins_every_from_and_records_the_id(config, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "Containerfile").write_text("FROM debian:bookworm-slim\nRUN apt-get update\nUSER 1000\n")
    debian = {"Id": "sha256:" + "f" * 64, "Digest": DIGEST, "RepoDigests": [f"docker.io/library/debian@{DIGEST}"],
              "Config": {}, "Os": "linux", "Architecture": "amd64"}
    podman = Podman({"docker.io/library/debian:bookworm-slim": debian}, BOOKWORM)
    built = {}
    original = podman.__call__

    def watch(argv, **kwargs):
        if argv[1] == "build":
            built["text"] = Path(argv[argv.index("--file") + 1]).read_text()
            built["argv"] = argv
        return original(argv, **kwargs)
    name, entry = sv.build("tools", project / "Containerfile", podman=["podman"], run=watch, out=lambda _: None)
    assert built["text"].startswith(f"FROM docker.io/library/debian@{DIGEST}\n")
    assert "--pull=never" in built["argv"] and built["argv"][-1] == str(project)
    assert entry["kind"] == "built" and entry["image"] == "localhost/sandbox-ai-image-tools:latest"
    assert entry["from"] == [{"ref": "debian:bookworm-slim", "pinned": f"docker.io/library/debian@{DIGEST}"}]
    assert entry["os"]["VERSION_CODENAME"] == "bookworm" and sv.base_problem(entry) is None
    assert sv.read_registry()["tools"]["containerfile_sha256"]
    with pytest.raises(ValueError, match="build of .* failed"):
        sv.build("tools2", project / "Containerfile", podman=["podman"], run=Podman(
            {"docker.io/library/debian:bookworm-slim": debian}, fail={"build"}), out=lambda _: None)


def test_build_refuses_what_an_agent_can_write_and_runs_without_network(config, tmp_path):
    """Before: a Containerfile or build context inside a tree a sandbox can write was only warned about, and its RUN
    steps reached the network straight from this host - outside the sandbox's firewall, the VPN and Tor - so an agent
    that changed the Containerfile in its workspace had its commands run with your address once you built it. Now
    such a file or folder is refused unless --trust-agent-written says it was reviewed, and RUN steps get no network
    unless --build-network gives them this host's (said, and recorded)."""
    project = tmp_path / "work"
    project.mkdir()
    (project / "Containerfile").write_text("FROM debian:bookworm-slim\nRUN true\n")
    debian = {"Id": "sha256:" + "f" * 64, "Digest": DIGEST, "RepoDigests": [f"docker.io/library/debian@{DIGEST}"],
              "Config": {}, "Os": "linux", "Architecture": "amd64"}
    podman = Podman({"docker.io/library/debian:bookworm-slim": debian}, BOOKWORM)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text(json.dumps([str(project)]))  # a workspace
    with pytest.raises(ValueError, match="sandbox can write.*--trust-agent-written"):
        sv.build("tools", project / "Containerfile", podman=["podman"], run=podman, out=lambda _: None)
    assert not any(c[1] in ("pull", "build") for c in podman.calls), "nothing pulled or built"
    outside = tmp_path / "mine"
    outside.mkdir()
    (outside / "Containerfile").write_text("FROM debian:bookworm-slim\nRUN true\n")
    with pytest.raises(ValueError, match="build context .*sandbox can write"):
        sv.build("tools", outside / "Containerfile", context=project, podman=["podman"], run=podman,
                 out=lambda _: None)
    said = []
    name, entry = sv.build("tools", project / "Containerfile", trust_agent_written=True, podman=["podman"],
                           run=podman, out=said.append)
    build = next(c for c in podman.calls if c[1] == "build")
    assert "--network=none" in build and entry["build_network"] is False and entry["agent_written"] is True
    assert any("sandbox can write" in line and "trusted" in line for line in said)
    said.clear()
    podman.calls.clear()
    name, entry = sv.build("tools", outside / "Containerfile", replace=True, network=True, podman=["podman"],
                           run=podman, out=said.append)
    build = next(c for c in podman.calls if c[1] == "build")
    assert not any(token.startswith("--network") for token in build) and entry["build_network"] is True
    assert any("outside the sandbox's firewall, VPN and Tor" in line for line in said)
    assert entry["agent_written"] is False
    # The command line: both options
    parsed = []
    original = sv.build
    sv.build = lambda *args, **kwargs: parsed.append(kwargs) or ("x", {})
    try:
        assert sv.main(["build-image", "x", "--file", "F", "--trust-agent-written", "--build-network"]) == 0
    finally:
        sv.build = original
    assert parsed[0]["trust_agent_written"] is True and parsed[0]["network"] is True
    # ... and sandbox-ai images build-image hands both on
    handed = []
    original_main = sv.main
    sv.main = lambda argv, **kwargs: handed.append(argv) or 0
    try:
        assert im.main(["build-image", "x", "--file", "F", "--trust-agent-written", "--build-network"]) == 0
        assert im.main(["build-image", "y", "--file", "F"]) == 0
    finally:
        sv.main = original_main
    assert "--trust-agent-written" in handed[0] and "--build-network" in handed[0]
    assert "--trust-agent-written" not in handed[1] and "--build-network" not in handed[1]


def test_base_problem():
    good = {"os": {"ID": "debian", "VERSION_CODENAME": "bookworm"}, "platform": "linux/amd64"}
    assert sv.base_problem(good) is None
    assert "Debian 12" in sv.base_problem({**good, "os": {"ID": "ubuntu", "VERSION_ID": "24.04"}})
    assert "unknown" in sv.base_problem({**good, "os": {}})
    assert "LD_PRELOAD" in sv.base_problem({**good, "loader_env": ["LD_PRELOAD"]})
    assert "windows" in sv.base_problem({**good, "platform": "windows/amd64"})


# -- the services' syntax --

def test_parse_spec_and_round_trip():
    service = sv.parse_spec("db:image=postgres,port=5432,port=8080,ready=8080,ready-timeout=90,env=POSTGRES_DB=app,"
                            "env=HOST_VAR,secret=POSTGRES_PASSWORD=pg,data=/var/lib/postgresql/data,keep=yes,"
                            "user=999,read-only=no,memory=1g,cpus=0.5,pids=512")
    assert service["image"] == "postgres" and service["ports"] == [5432, 8080] and service["ready"] == 8080
    assert service["env"] == [{"name": "POSTGRES_DB", "value": "app"}, {"name": "HOST_VAR", "from_host": True}]
    assert service["secrets"] == [{"name": "POSTGRES_PASSWORD", "secret": "pg"}] and service["user"] == "999:999"
    assert service["keep"] and not service["read_only"] and service["memory"] == "1g" and service["pids"] == 512
    assert sv.parse_spec(sv.spec_text(service)) == service
    plain = sv.parse_spec("cache")
    assert plain == sv.default_service("cache") and sv.spec_text(plain) == "cache"
    assert sv.ready_port(plain) is None and sv.ready_port(sv.parse_spec("c:port=1,port=2")) == 1
    assert sv.ready_port(sv.parse_spec("c:port=1,ready=none")) is None
    assert sv.spec_text(sv.parse_spec("c:port=1,ready=1")) == "c:port=1"


@pytest.mark.parametrize("value, match", [
    ("", "NAME"), ("Db", "service name"), ("db:port", "KEY=VALUE"), ("db:colour=red", "KEY=VALUE"),
    ("db:port=0", "TCP port"), ("db:port=70000", "TCP port"), ("db:port=1,port=1", "twice"),
    ("db:ready=5", "none of its ports"), ("db:ready-timeout=0", "1 to 600"), ("db:env=1A=b", "env="),
    ("db:env=A=x\x01", "env="), ("db:env=A=1,secret=A", "twice"), ("db:secret=A=b-c", "secret="),
    ("db:data=relative", "absolute"), ("db:data=/proc/x", "absolute"), ("db:data=/etc", "system folder"),
    ("db:data=/a/../etc", "system folder"), ("db:data=/x,data=/x", "twice"), ("db:keep=maybe", "yes or no"),
    # Two leading slashes: os.path.normpath keeps them (POSIX), and //etc, //proc were no system folder before.
    ("db:data=//etc", "system folder"), ("db:data=//proc", "system folder"), ("db:data=//dev/x", "system folder"),
    ("db:data=/x,data=//x", "twice"),
    ("db:keep=yes", "data folders"), ("db:user=redis", "UID"), ("db:memory=lots", "size"), ("db:cpus=-1", "number"),
    ("db:pids=8", "at least 16"), ("db:image=../x", "your images"),
])
def test_parse_spec_refuses(value, match):
    with pytest.raises(ValueError, match=match):
        sv.parse_spec(value)


# -- the launcher's checks --

def start(*options, tor=False):
    a = s.parse_args(["claude", *options])
    a.vpn_profile = {"type": "tor", "name": "t"} if tor else None
    a.run_id = "claude-1234abcd"
    a.keep_home = False
    return a


def test_check_services(config):
    add_redis()
    a = start("--service", "cache:image=redis,port=6379")
    found = sv.check(a, s)
    assert [x["name"] for x in found] == ["cache"] and a.services[0]["user"] == "999:1000"
    assert a.services[0]["record"]["image"] == f"docker.io/library/redis@{DIGEST}"
    plain = start()
    assert sv.check(plain, s) == [] and plain.services == []
    notes = sv.notes(a)
    assert "UNCHECKED" in notes[0] and "localhost:6379" in notes[0]
    low = start("--service", "w:image=redis,port=80,read-only=no")
    sv.check(low, s)
    assert any("below 1024" in note for note in sv.notes(low)) and any("writable" in n for n in sv.notes(low))


@pytest.mark.parametrize("options, tor, match", [
    (["--backend", "bwrap"], False, "Podman backend"),
    (["--podman-url", "unix:///x.sock"], False, "this machine's Podman"),
    ([], True, "behind Tor"),
])
def test_check_services_refuses_the_setups(config, options, tor, match):
    add_redis()
    with pytest.raises(ValueError, match=match):
        sv.check(start(*options, "--service", "cache:image=redis", tor=tor), s)


@pytest.mark.parametrize("spec, extra, match", [
    ("cache:image=gone", [], "no image gone of yours"),
    ("cache:image=redis,user=1", [], "connection proxy's"),
    ("cache:image=redis,user=950:950", [], "tor's"),
    ("cache:image=redis,user=2", [], "sound server's"),
    ("cache:image=redis,port=15001", [], "connection proxy"),
    ("cache:image=redis,port=53", [], "DNS"),
    ("cache:image=redis,port=9040", [], "Tor"),
    ("cache:image=redis,port=11434", ["--allow-host-port", "11434"], "port of the host"),
])
def test_check_services_refuses_the_firewalls_own(config, spec, extra, match):
    add_redis()
    with pytest.raises(ValueError, match=match):
        sv.check(start(*extra, "--service", spec), s)


def test_check_services_edges(config):
    add_redis()
    with pytest.raises(ValueError, match="given twice"):
        sv.check(start("--service", "a:image=redis", "--service", "a:image=redis"), s)
    with pytest.raises(ValueError, match="the service a's too"):
        sv.check(start("--service", "a:image=redis,port=6379", "--service", "b:image=redis,port=6379"), s)
    with pytest.raises(ValueError, match="at most 8"):
        sv.check(start(*[w for i in range(9) for w in ("--service", f"s{i}:image=redis")]), s)
    found = sv.read_registry()
    found["redis"] = {**found["redis"], "user": None, "config_user": "nobody"}
    with pytest.raises(ValueError, match="set it as a number"):
        sv.check(start("--service", "a:image=redis"), s, found)
    assert sv.check(start("--service", "a:image=redis,user=1000"), s, found)[0]["user"] == "1000:1000"
    with pytest.raises(ValueError, match="an agent's start"):
        a = start("--service", "a:image=redis")
        a.helper_of = "lead"
        sv.check(a, s)


# -- the run line --

def test_run_command(config, tmp_path, monkeypatch):
    add_redis()
    monkeypatch.setenv("HOST_VAR", "from-host")
    a = start("--service", "cache:image=redis,port=6379,data=/data,env=MODE=dev,env=HOST_VAR,secret=PW=redis_pw")
    sv.check(a, s)
    a.services[0]["secrets"][0]["value"] = "s3cret"
    argv, env = sv.create_command(a, "agent-claude-1234abcd", a.services[0], tmp_path, s, {"memory", "cpus", "pids"})
    assert argv[:2] == ["podman", "create"] and "--detach" not in argv and "--pull=never" in argv
    for flag in ("--cap-drop=ALL", "--security-opt=no-new-privileges", "--read-only", "--init"):
        assert flag in argv
    joined = " ".join(argv)
    assert "--pod agent-claude-1234abcd" in joined and "--name agent-claude-1234abcd-service-cache" in joined
    assert "--user 999:1000" in joined and "--memory 512m" in joined and "--pids-limit 256" in joined
    assert f"type=bind,src={tmp_path}/resolv.conf,dst=/etc/resolv.conf,ro,nodev" in joined
    assert f"type=bind,src={tmp_path}/hosts,dst=/etc/hosts,ro,nodev" in joined
    assert "--tmpfs /data:rw,noexec,nosuid,nodev,size=512m" in joined
    assert "--env MODE=dev" in joined and "--env HOST_VAR" in joined and "--env PW" in joined
    assert "s3cret" not in joined and "from-host" not in joined  # values only in the environment
    assert env == {"HOST_VAR": "from-host", "PW": "s3cret"}
    assert argv[-1] == ID and "--privileged" not in argv and "--cap-add" not in joined
    assert "--network" not in joined  # the pod's: behind the sandbox's firewall
    argv, _ = sv.create_command(a, "pod", a.services[0], tmp_path, s, set())
    assert "--memory" not in argv and "--cpus" not in argv  # not enforceable here: as the agent's limits


def test_kept_data_volumes(config, tmp_path):
    add_redis()
    a = start("--service", "db:image=redis,data=/data,data=/logs,keep=yes")
    sv.check(a, s)
    assert sv.data_mounts(a, a.services[0]) == [("/data", "tmpfs", None), ("/logs", "tmpfs", None)]  # not kept
    a.keep_home, a.start_key = True, "brisk-otter-1"
    assert sv.data_mounts(a, a.services[0]) == [("/data", "volume", "sandbox-ai-svc-brisk-otter-1_db-0"),
                                                ("/logs", "volume", "sandbox-ai-svc-brisk-otter-1_db-1")]
    argv, _ = sv.create_command(a, "pod", a.services[0], tmp_path, s, set())
    assert "sandbox-ai-svc-brisk-otter-1_db-0:/data:rw,nosuid,nodev,U" in argv
    podman = Podman()
    podman.calls = []

    def volumes(argv, **kwargs):
        podman.calls.append(argv)
        if argv[1:3] == ["volume", "ls"]:
            assert "label=sandbox-ai.service-volume=brisk-otter-1" in argv
            return subprocess.CompletedProcess(argv, 0, "sandbox-ai-svc-brisk-otter-1_db-0\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    assert sv.forget_volumes("brisk-otter-1", ["podman"], volumes) == ["sandbox-ai-svc-brisk-otter-1_db-0"]


def test_kept_data_volumes_of_two_starts_never_share_a_name(config):
    """A start's key and a service's name both may hold '-': the kept volume of the start brisk-otter's service
    lantern-db must not be the one of the start brisk-otter-lantern's service db - that start would find (podman
    volume exists) and use the other's data."""
    add_redis()
    first = start("--service", "lantern-db:image=redis,data=/data,keep=yes")
    second = start("--service", "db:image=redis,data=/data,keep=yes")
    sv.check(first, s)
    sv.check(second, s)
    first.keep_home, first.start_key = True, "brisk-otter"
    second.keep_home, second.start_key = True, "brisk-otter-lantern"
    names = {sv.data_mounts(a, a.services[0])[0][2] for a in (first, second)}
    assert len(names) == 2, names
    # Every name is still one podman takes, and the key's label finds them (sandbox-ai delete).
    for name in names:
        assert len(name) <= 255 and __import__("re").fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name)


# -- the handshake with the supervisor --

INFRA_ID = "9" * 64


def userns_answers(run, modes=("", "")):
    """RUN, with podman's answers to the user namespace check of a service (services.check_userns): the pod's infra
    container, the PIDs of it and the service (this host's engine), or their UsernsMode (another engine's: MODES)."""
    def answer(argv, **kwargs):
        args = argv[1:]
        if args[:2] == ["pod", "inspect"] and "{{.InfraContainerID}}" in args:
            return subprocess.CompletedProcess(argv, 0, INFRA_ID + "\n", "")
        if args[:3] == ["inspect", "--type", "container"]:
            if "{{.State.Pid}}" in args:
                return subprocess.CompletedProcess(argv, 0, "100\n200\n", "")
            if "{{.HostConfig.UsernsMode}}" in args:
                return subprocess.CompletedProcess(argv, 0, "\n".join(modes) + "\n", "")
        return run(argv, **kwargs)
    return answer


def same_userns(path):
    return "user:[4026531837]"


def other_userns(path):
    return "user:[4026531837]" if path == "/proc/100/ns/user" else "user:[4026532999]"


@pytest.mark.parametrize("local,readlink,modes,ok", [
    (True, same_userns, None, True),
    (True, other_userns, None, False),      # e.g. userns=keep-id in containers.conf: its uid 0 is the proxy's 1 there
    (False, None, ("", ""), True),          # macOS (podman machine): the agent's container and the service's alike
    (False, None, ("", "keep-id"), False),
])
def test_a_service_runs_in_the_pods_user_namespace(config, tmp_path, monkeypatch, local, readlink, modes, ok):
    """The firewall knows the proxy's (and tor's) packets by their uid as the pod's network namespace sees it
    (meta skuid): a service in another user namespace (containers.conf's userns = keep-id, auto) would be other uids
    there - its root the proxy's uid 1, past the redirect to the proxy. Podman refuses --userns with --pod, so it is
    proven after the start - the same user namespace as the pod's infra container (this host's engine: their
    /proc/PID/ns/user), else the service is removed and the run does not start."""
    add_redis()
    a = start("--service", "cache:image=redis,port=6379")
    sv.check(a, s)
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    monkeypatch.setattr(s, "local_engine", lambda _a: local)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "id\n", "")
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, userns_answers(run, modes or ("", "")),
                       out=lambda _: None, readlink=readlink or (lambda path: pytest.fail("read " + path)))
    assert answer["ok"] is ok, answer
    if not ok:
        assert "user namespace is not the" in answer["error"], answer
        assert ["podman", "rm", "--force", "--time", "0", "agent-x-service-cache"] in calls, "removed at once"

def test_launch_waits_for_the_supervisor_and_answers(config, tmp_path):
    add_redis()
    a = start("--service", "cache:image=redis,port=6379", "--service", "two:image=redis")
    sv.check(a, s)
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    calls, said = [], []
    ticks = iter(range(10_000))

    def fake(argv, **kwargs):
        calls.append(argv)
        assert argv[1] != "create" or kwargs["env"] is not None  # its secrets and variables: the environment
        return subprocess.CompletedProcess(argv, 0, "id\n", "")

    def sleep(_):
        if next(ticks) == 3:
            (a.control_dir / s.RUNTIME.SERVICES_GO).touch()  # the firewall and the proxy are up
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, userns_answers(fake), sleep=sleep, out=said.append,
                       readlink=same_userns)
    assert answer == {"ok": True, "services": ["cache", "two"]}
    assert [c[1] for c in calls] == ["create", "init", "start"] * 2  # each created, initialised, then started
    assert json.loads((a.control_dir / s.RUNTIME.SERVICES_STARTED).read_text()) == answer
    assert "podman logs agent-x-service-cache" in said[0]


def test_launch_answers_when_the_limits_cannot_be_read(config, tmp_path, monkeypatch):
    """Whatever fails after SERVICES_GO - asking which limits can be enforced here too - the supervisor gets an answer
    at once (it would wait SERVICES_START_SECONDS for none, and the thread would end with a traceback)."""
    add_redis()
    a = start("--service", "cache:image=redis")
    sv.check(a, s)
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()

    def broken(_a, _l):
        raise OSError("cgroup.controllers: permission denied")
    monkeypatch.setattr(sv, "enforceable", broken)
    answer = sv.launch(a, "pod", tmp_path, lambda: True, s, lambda *x, **k: pytest.fail("ran"), out=lambda _: None)
    assert answer["ok"] is False and "permission denied" in answer["error"]
    assert json.loads((a.control_dir / s.RUNTIME.SERVICES_STARTED).read_text()) == answer


def test_launch_failures(config, tmp_path):
    add_redis()
    a = start("--service", "cache:image=redis")
    sv.check(a, s)
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    # the run ended before its supervisor was ready: nothing is started
    assert sv.launch(a, "pod", tmp_path, lambda: False, s, lambda *x, **k: pytest.fail("ran")) is None
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    answer = sv.launch(a, "pod", tmp_path, lambda: True, s,
                       lambda argv, **k: subprocess.CompletedProcess(argv, 125, "", "Error: no such pod"),
                       out=lambda _: None)
    assert answer["ok"] is False and "cache did not start" in answer["error"] and "no such pod" in answer["error"]
    assert json.loads((a.control_dir / s.RUNTIME.SERVICES_STARTED).read_text())["ok"] is False


def test_collect_logs(config, tmp_path):
    add_redis()
    a = start("--service", "cache:image=redis", "--log-dir", str(tmp_path / "logs"))
    sv.check(a, s)
    written = sv.collect_logs(a, "pod", s, lambda argv, **k: subprocess.CompletedProcess(argv, 0, b"ready\n", b"warn\n"))
    assert written and written[0].read_bytes() == b"ready\nwarn\n" and written[0].name.startswith("service-cache-")
    a.log_dir = None
    assert sv.collect_logs(a, "pod", s, lambda *x, **k: pytest.fail("ran")) == []


def test_prepare_checks_the_image_and_secrets(config, monkeypatch):
    add_redis()
    a = start("--service", "cache:image=redis,secret=PW")
    sv.check(a, s)
    store = type("Store", (), {"get": staticmethod(lambda name: {"PW": "v"}.get(name))})
    monkeypatch.setattr(s, "load_secret_store_module", lambda: store)
    sv.prepare(a, s, lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))
    assert a.services[0]["secrets"][0]["value"] == "v"
    with pytest.raises(ValueError, match="nothing is pulled at a start"):
        sv.prepare(a, s, lambda argv, **k: subprocess.CompletedProcess(argv, 1, "", ""))
    a = start("--service", "cache:image=redis,secret=GONE")
    sv.check(a, s)
    with pytest.raises(ValueError, match="no secret GONE"):
        sv.prepare(a, s, lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))
    a = start("--service", "cache:image=redis,env=NOT_SET_ANYWHERE")
    monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
    sv.check(a, s)
    with pytest.raises(ValueError, match="no NOT_SET_ANYWHERE"):
        sv.prepare(a, s, lambda argv, **k: subprocess.CompletedProcess(argv, 0, "", ""))


def test_plan_and_records(config):
    add_redis()
    a = start("--service", "cache:image=redis,port=6379,secret=PW")
    sv.check(a, s)
    plan = sv.plan(a, s)[0]
    assert plan["unchecked"] and plan["reachable_at"] == ["localhost:6379"] and plan["secrets"] == ["PW"]
    assert plan["ready"] == 6379 and "firewall" in plan["network"]
    assert sv.supervisor_config(a) == [{"name": "cache", "ready": 6379, "timeout": 60}]
    assert sv.record_entries(a, "pod")[0]["container"] == "pod-service-cache"


# -- a variant on a base image of yours --

def add_bookworm(name="pyimg"):
    data = {**REDIS, "RepoDigests": [f"docker.io/library/python@{DIGEST}"], "Config": {"User": ""}}
    podman = Podman({"docker.io/library/python:3.12-bookworm": data, f"docker.io/library/python@{DIGEST}": data},
                    BOOKWORM)
    return sv.pull("python:3.12-bookworm", name, podman=["podman"], run=podman, out=lambda _: None)


def test_variant_on_a_base_image_of_yours(config):
    add_bookworm()
    add_redis()
    with pytest.raises(ValueError, match="Debian 12"):
        im.new_variant("cache", "claude", base="redis")
    description = im.new_variant("py", "claude", base="pyimg")
    assert description["base_image"] == "pyimg" and description["base"] == "localhost/claude-sandbox-py:base"
    assert "FROM localhost/claude-sandbox-py:base\n" in description["containerfile"]
    assert im.build_plan("py") == ("claude", "based", 0)
    summary = im.base_summary("py")
    assert summary["name"] == "pyimg" and summary["unchecked"] and "UNCHECKED" in summary["warning"]
    assert sv.users_of("pyimg") == ["py"]
    with pytest.raises(ValueError, match="base of the variant"):
        sv.remove("pyimg", podman=["podman"], run=Podman())
    with pytest.raises(ValueError, match="keeps sandbox-ai's base"):
        im.extend("claude", base="pyimg")
    with pytest.raises(ValueError, match="name of one of your images"):
        im.new_variant("pyimg", "claude")
    described, changes = im.extend("py", base="")
    assert changes == ["base: the agent's own"] and described["base"] == "localhost/claude-sandbox:base"
    assert im.base_summary("py") is None
    described, changes = im.extend("py", base="pyimg")
    assert changes == ["base pyimg"]
    # the launcher's view: read from the variant's file only
    assert s.variant_base(s.parse_args(["claude", "--image-variant", "py"])) == "pyimg"
    assert s.variant_base(s.parse_args(["claude"])) is None


def test_prep_and_build_layers(config, capsys):
    add_bookworm()
    im.new_variant("py", "claude", base="pyimg")
    text = im.prep_containerfile("py", "claude", "pyimg", sv.record("pyimg"))
    assert f"FROM docker.io/library/python@{DIGEST}\nUSER root\nRUN set -eu;" in text
    assert "ld.so.preload" in text and "bookworm" in text and "--reinstall python3-minimal" in text
    assert "nodejs npm" in text and "UNCHECKED" in text
    # The interpreter and its standard library themselves (python3-minimal is only its links), and no zip of the
    # standard library's name, which stands before it on sys.path even with -I -S.
    assert "python3.11-minimal libpython3.11-minimal libpython3.11-stdlib" in im.PREP_SCRIPT
    assert "/usr/lib/python311.zip" in im.PREP_SCRIPT
    assert subprocess.run(["sh", "-n", "-c", im.PREP_SCRIPT]).returncode == 0
    podman = Podman({f"docker.io/library/python@{DIGEST}": REDIS})
    assert im.build_prep("py", run=podman) == "localhost/claude-sandbox-py:prep"
    build = next(call for call in podman.calls if call[1] == "build")
    assert "--pull=never" in build and "localhost/claude-sandbox-py:prep" in build
    replaced = Podman({f"docker.io/library/python@{DIGEST}": {**REDIS, "Id": "sha256:" + "9" * 64}})
    with pytest.raises(ValueError, match="not what was recorded"):
        im.build_prep("py", run=replaced)
    with pytest.raises(ValueError, match="built on this machine"):
        im.build_prep("py", podman_url="unix:///x")
    # the agent's own build leaves the variant on your image to its own build
    base = {"Id": "sha256:base", "Config": {}}
    store = Podman({"localhost/claude-sandbox:base": base})
    lines = []
    assert im.build_layers("claude", with_variants=True, run=store, out=lines.append) == 0
    assert any("built on your image pyimg" in line for line in lines)
    # its own layer goes on its own base
    store = Podman({"localhost/claude-sandbox:base": base, "localhost/claude-sandbox-py:base": {"Id": "sha256:own"}})
    assert im.build_layers("py", run=store, out=lambda _: None) == 0
    build = next(call for call in store.calls if call[1] == "build")
    assert "sandbox-ai.base-id=sha256:own" in build
    assert im.build_layers("py", run=Podman({"localhost/claude-sandbox:base": base}), out=lambda _: None) == 1
    assert "no base image localhost/claude-sandbox-py:base" in capsys.readouterr().err


def test_images_commands(config, capsys):
    podman = Podman({"docker.io/library/redis:7-alpine": REDIS, f"docker.io/library/redis@{DIGEST}": REDIS}, FILES)
    assert im.main(["pull", "redis:7-alpine", "--as", "cache", "--json"], run=podman) == 0
    out = capsys.readouterr()
    assert json.loads(out.out)["name"] == "cache" and "UNCHECKED" in out.err
    assert im.main(["list", "--json"], run=podman) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "cache"
    assert im.main(["list"], run=podman) == 0
    assert "UNCHECKED" in capsys.readouterr().out
    assert im.main(["--podman-url", "unix:///x", "pull", "redis:7-alpine"], run=podman) == 1
    assert "recorded on this machine" in capsys.readouterr().err
    assert im.main(["remove", "cache"], run=podman) == 0
    assert "forgot your image cache" in capsys.readouterr().out
    add_bookworm()
    assert im.main(["new", "py", "--from", "claude", "--base", "pyimg"], run=podman) == 0
    assert "UNCHECKED" in capsys.readouterr().out
    assert im.main(["extend", "py", "--no-base"], run=podman) == 0
    assert "changed: base: the agent's own" in capsys.readouterr().out
    assert im.main(["extend", "py", "--base", "pyimg", "--no-base"], run=podman) == 1
    assert im.main(["build-plan", "py"], run=podman) == 0
    assert capsys.readouterr().out.strip().endswith("claude variant 0")


# -- the launcher: dry run, run.json, modes --

def dry_run(capsys, *options):
    assert s.main(["claude", "--dry-run", *options]) == 0
    captured = capsys.readouterr()
    return json.loads(captured.out), captured.err


def test_dry_run_shows_services_and_bases(config, capsys, tmp_path, monkeypatch):
    (tmp_path / "work").mkdir()
    monkeypatch.chdir(tmp_path / "work")
    add_redis()
    plan, err = dry_run(capsys, "--service", "cache:image=redis,port=6379")
    assert plan["services"][0]["name"] == "cache" and plan["services"][0]["unchecked"] and plan["image_base"] is None
    assert "UNCHECKED" in err
    add_bookworm()
    im.new_variant("py", "claude", base="pyimg")
    plan, err = dry_run(capsys, "--image-variant", "py")
    assert plan["image_base"]["name"] == "pyimg" and "UNCHECKED" in err and plan["services"] == []
    with pytest.raises(ValueError, match="paranoid"):
        s.main(["claude", "--dry-run", "--mode", "paranoid", "--service", "cache:image=redis"])
    with pytest.raises(ValueError, match="nobody reviewed"):
        s.main(["claude", "--dry-run", "--mode", "high", "--image-variant", "py"])
    plan, _ = dry_run(capsys, "--mode", "high", "--service", "cache:image=redis")
    assert plan["services"][0]["name"] == "cache"


def test_starts_lists_services():
    st = load("starts_for_services_tests", "sandbox/starts.py")
    record = {"services": [{"name": "db", "image": "postgres", "ports": [5432, "x"], "container": "c"},
                           {"name": "BAD"}, "junk"]}
    found = st.record_services(record)
    assert found == [{"name": "db", "image": "postgres", "reference": None, "ports": [5432], "container": "c",
                      "unchecked": True}]
    assert st.services_text({"services": found}) == "services: db:5432 (unchecked images)"
    assert st.services_text({}) == "" and st.record_services({"services": 5}) == []


def test_the_agent_is_told_its_services(config):
    add_redis()
    a = start("--service", "cache:image=redis,port=6379", "--service", "other:image=redis")
    sv.check(a, s)
    assert s.agent_environment(a)["SANDBOX_AI_SERVICES"] == "cache=localhost:6379 other="
    assert "SANDBOX_AI_SERVICES" not in s.agent_environment(start())


FAKE_PODMAN = r'''#!/usr/bin/env python3
import json, os, sys
argv = sys.argv[1:]
while argv and argv[0].startswith("--cgroup-manager"):
    argv = argv[1:]
with open(os.environ["PODMAN_LOG"], "a") as log:
    log.write(json.dumps(argv) + "\n")
if argv[:2] == ["image", "inspect"]:
    print(json.dumps([{"Id": os.environ["FAKE_IMAGE_ID"], "Config": {}}]))
sys.exit(0)
'''


def test_sandbox_ai_build_of_a_variant_on_your_image(config, tmp_path):
    """build_image.sh: your image prepared (nothing pulled), uv's image pulled as other builds refresh it, the agent's
    layers built on the prepared image with --pull=never, then the variant's layer on its own base."""
    add_bookworm()
    im.new_variant("py", "claude", base="pyimg")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "podman").write_text(FAKE_PODMAN)
    (bin_dir / "podman").chmod(0o755)
    log = tmp_path / "podman.jsonl"
    env = {**os.environ, "PATH": f"{bin_dir}:/usr/bin:/bin", "PODMAN_LOG": str(log),
           "FAKE_IMAGE_ID": "sha256:" + ID, "SANDBOX_AI_PYTHON": sys.executable,
           "SANDBOX_AI_BUILD_NO_DNS_CHECK": "1"}
    done = subprocess.run(["bash", str(ROOT / "sandbox" / "build_image.sh"), "--pin", "2.1.0", "py"], env=env,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    builds = [call for call in calls if call[0] == "build"]
    prep, agent_layers, extension = builds
    assert "--pull=never" in prep and prep[prep.index("--tag") + 1] == "localhost/claude-sandbox-py:prep"
    assert ["pull", "ghcr.io/astral-sh/uv:latest"] in calls
    assert not any(call[0] == "pull" and "python" in " ".join(call) for call in calls)  # your image: never pulled
    assert "--pull=never" in agent_layers and "NODE_IMAGE=localhost/claude-sandbox-py:prep" in agent_layers
    assert agent_layers[agent_layers.index("--tag") + 1] == "localhost/claude-sandbox-py:base"
    assert "CLAUDE_VERSION=2.1.0" in agent_layers and agent_layers[agent_layers.index("--file") + 1].endswith(
        "Containerfile.claude")
    assert extension[extension.index("--tag") + 1] == "localhost/claude-sandbox-py:latest"
    assert "localhost/claude-sandbox:base" not in " ".join(agent_layers)
    refused = subprocess.run(["bash", str(ROOT / "sandbox" / "build_image.sh"), "--node", "x", "py"], env=env,
                             capture_output=True, text=True, timeout=120)
    assert refused.returncode == 2 and "built on your own image" in refused.stderr


def test_a_start_with_a_service(config, tmp_path, monkeypatch):
    """A real start (the pod replaced): the images checked before anything runs, the supervisor told what to wait
    for, run.json naming the service, the agent its address."""
    add_redis()
    monkeypatch.setattr(s.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(s.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(s, "prepare_home", lambda *args: [])
    monkeypatch.setattr(s, "resolve_pod_hosts", lambda a, hosts: {host: ["203.0.113.5"] for host in hosts})
    prepared, seen = [], {}
    monkeypatch.setattr(sv, "prepare", lambda a, l=None, run=None: prepared.append([x["name"] for x in a.services]))
    monkeypatch.setattr(s, "load_services_module", lambda: sv)

    def run(a, session, home):
        seen["config"] = json.loads((session / "config.json").read_text())
        seen["record"] = json.loads((a.control_dir / "run.json").read_text())
        return 0
    monkeypatch.setattr(s, "run_pod", run)
    work = tmp_path / "work"
    assert s.main(["claude", "--workspace", str(work), "--no-auth", "--no-tmux", "--no-keep",
                   "--service", "cache:image=redis,port=6379,ready-timeout=30"]) == 0
    assert prepared == [["cache"]]
    assert seen["config"]["services"] == [{"name": "cache", "ready": 6379, "timeout": 30}]
    assert seen["config"]["env"]["SANDBOX_AI_SERVICES"] == "cache=localhost:6379"
    assert seen["record"]["services"][0]["name"] == "cache" and seen["record"]["services"][0]["unchecked"]
    with pytest.raises(ValueError, match="Podman backend"):
        s.main(["claude", "--workspace", str(work), "--backend", "bwrap", "--service", "cache:image=redis"])


def test_run_pod_starts_the_services_and_keeps_their_output(config, tmp_path, monkeypatch):
    """The pod's run: the services' half of the handshake in a thread beside the agent's container, their output
    kept before the pod (and they) are removed."""
    add_redis()
    a = start("--service", "cache:image=redis", "--log-dir", str(tmp_path / "logs"))
    sv.check(a, s)
    order, launched = [], threading.Event()
    monkeypatch.setattr(s, "load_services_module", lambda: sv)
    monkeypatch.setattr(sv, "launch", lambda a, pod, session, alive, l: (order.append(("launch", pod)),
                                                                          launched.set()))
    monkeypatch.setattr(sv, "collect_logs", lambda a, pod, l: order.append(("logs", pod)) or [])
    monkeypatch.setattr(s.subprocess, "run", lambda argv, **k: order.append(tuple(argv[:3])) or
                        subprocess.CompletedProcess(argv, 0, "", ""))

    class Child:
        def poll(self):
            return None
    monkeypatch.setattr(s.subprocess, "Popen", lambda argv: Child())
    monkeypatch.setattr(s, "wait_child", lambda child: launched.wait(5) and 0)
    assert s.run_pod(a, tmp_path, tmp_path) == 0
    pod = s.pod_name(a)
    assert ("launch", pod) in order and order.index(("logs", pod)) < order.index(("podman", "pod", "rm"))
    a.probe = True  # a probe runs no services
    order.clear()
    launched.set()
    s.run_pod(a, tmp_path, tmp_path)
    assert not any(item[0] == "launch" for item in order)


def test_a_session_with_services_waits_longer_for_its_start():
    se = load("sessions_for_services_tests", "sandbox/sessions.py")
    assert se.services_wait(s.parse_args(["claude"])) == 0 and se.services_wait(None) == 0
    a = s.parse_args(["claude", "--service", "db:port=5432,ready-timeout=200", "--service", "cache"])
    assert se.services_wait(a) == 200 + 30 + 60 + 30


def test_the_listing_of_images_names_yours(config, capsys):
    add_bookworm()
    im.new_variant("py", "claude", base="pyimg")
    podman = Podman({"localhost/claude-sandbox-py:latest": {"Id": "sha256:x", "Created": "2026-11-12T10:00:00",
                                                             "Size": 1, "Config": {"Labels": {}}}})
    assert im.main([], run=podman) == 0
    out = capsys.readouterr().out
    assert "on your image pyimg (UNCHECKED)" in out and "your images: pyimg (UNCHECKED" in out
    assert next(row for row in im.images(run=podman) if row["target"] == "py")["base_image"] == "pyimg"


# -- behind the start's VPN: a service in the agent's pod is proven to share the VPN container's namespace --

INFRA, VPN_ID, SERVICE_ID = "1" * 64, "2" * 64, "3" * 64


def pod_podman(service_network, vpn_running=True, killswitch=True):
    """A fake `podman` for the start of the service cache in pod agent-x, whose VPN container is agent-x-vpn
    (vpn.agent_up names it so); returns (run, the commands it was given)."""
    calls, started = [], []
    containers = {"agent-x-vpn": (VPN_ID, vpn_running, f"container:{INFRA}", "corp"),
                  "agent-x-service-cache": (SERVICE_ID, started, service_network, "")}

    def run(argv, **kwargs):
        calls.append(argv)
        args = argv[1:]
        if args[:1] == ["start"]:
            started.append(True)  # it runs from its start on (created and initialised before: not running)
        if args[:2] == ["pod", "inspect"]:
            return subprocess.CompletedProcess(argv, 0, INFRA + "\n", "")
        if args[:2] == ["container", "inspect"]:
            cid, running, network, profile = containers[args[-1]]
            state = "true" if running else "false"
            return subprocess.CompletedProcess(argv, 0, f"{cid}|{state}|t|{network}|{profile}\n", "")
        if args[:3] == ["inspect", "--type", "container"] and "{{.State.Pid}}" in args:
            return subprocess.CompletedProcess(argv, 0, "100\n200\n", "")
        if args[0] == "exec":  # `nft list table inet sandbox_ai_vpn` in the VPN container
            listing = "table inet sandbox_ai_vpn {\n" + "".join(
                f" chain {h} {{ type filter hook {h} priority -5; policy drop;\n }}\n"
                for h in ("output", "input", "forward")) + "}\n"
            return subprocess.CompletedProcess(argv, 0 if killswitch else 1, listing if killswitch else "",
                                               "" if killswitch else "Error: No such file or directory")
        return subprocess.CompletedProcess(argv, 0, "id\n", "")
    return run, calls


@pytest.mark.parametrize("vpn,network,vpn_running,killswitch,ok", [
    (None, "pasta", True, True, True),                     # no VPN: nothing to prove (the pod is the sandbox's)
    ("corp", f"container:{INFRA}", True, True, True),       # in the pod's namespace, which the VPN's kill switch holds
    ("corp", "pasta", True, True, False),                  # a network of its own: past the VPN
    ("corp", f"container:{'4' * 64}", True, True, False),   # another container's namespace
    ("corp", f"container:{INFRA}", False, True, False),     # the VPN container is not running
    ("corp", f"container:{INFRA}", True, False, False),     # its kill switch is not in place
])
def test_a_service_behind_the_starts_vpn_runs_only_in_its_namespace(config, tmp_path, vpn, network, vpn_running,
                                                                     killswitch, ok):
    """--vpn with --service: each service, once started in the agent's pod, must show the pod's namespace - the VPN
    container's, with its kill switch (vpn.check_pod_vpn, MEMBER) - or it is removed and the run does not start."""
    add_redis()
    a = start("--service", "cache:image=redis,port=6379")
    sv.check(a, s)
    a.vpn = vpn
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    run, calls = pod_podman(network, vpn_running, killswitch)
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, run, out=lambda _: None, readlink=same_userns)
    started = calls[0]
    assert started[1] == "create" and started[started.index("--pod") + 1] == "agent-x"
    assert not any(token.startswith(("--network", "--net=")) for token in started), "only the pod's network"
    assert answer["ok"] is ok, answer
    assert bool([c for c in calls if c[1:3] == ["container", "inspect"]]) == bool(vpn), "proven exactly with a VPN"
    if not ok:
        assert "not behind the VPN corp" in answer["error"] and answer.get("services") is None
        assert ["podman", "rm", "--force", "--time", "0", "agent-x-service-cache"] in calls, "stopped at once"
        assert json.loads((a.control_dir / s.RUNTIME.SERVICES_STARTED).read_text())["ok"] is False


@pytest.mark.parametrize("vpn,network,readlink,ok", [
    ("corp", f"container:{INFRA}", "same", True),
    ("corp", "pasta", "same", False),                    # past the VPN
    (None, "pasta", "other", False),                     # another user namespace (its root the proxy's uid there)
])
def test_nothing_of_a_service_runs_before_it_is_checked(config, tmp_path, monkeypatch, vpn, network, readlink, ok):
    """Before: podman run --detach started the service, and its user namespace and network were proven only then - a
    moment in which a service outside the pod's user namespace or the VPN's network namespace ran. Now it is created
    and initialised (its namespaces made, nothing of the image run: podman init), proven, and only then started; a
    service that fails a check is removed without ever being started."""
    add_redis()
    a = start("--service", "cache:image=redis,port=6379")
    sv.check(a, s)
    a.vpn = vpn
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    monkeypatch.setattr(s, "local_engine", lambda _a: True)
    run, calls = pod_podman(network)
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, run, out=lambda _: None,
                       readlink=same_userns if readlink == "same" else other_userns)
    assert answer["ok"] is ok, answer
    verbs = [c[1] for c in calls]
    assert "run" not in verbs, "never run before it is checked"
    created, initialised = verbs.index("create"), verbs.index("init")
    checks = [i for i, c in enumerate(calls) if c[1] == "inspect" and "{{.State.Pid}}" in c
              or c[1:3] == ["container", "inspect"]]
    assert created < initialised < min(checks)
    if ok:
        assert ["podman", "start", "agent-x-service-cache"] in calls
        assert verbs.index("start") > max(i for i in checks if i < verbs.index("start"))
    else:
        assert "start" not in verbs, "a service that fails a check is never started"
        assert ["podman", "rm", "--force", "--time", "0", "agent-x-service-cache"] in calls


@pytest.mark.parametrize("network,ok", [(f"container:{INFRA}", True), ("pasta", False)])
def test_a_service_behind_a_tailnet_only_vpn(config, tmp_path, network, ok):
    """A Tailscale profile without an exit node loads no kill switch (vpn_runtime.up_tailscale: only the tailnet goes
    through it, the agent's other connections leave as without a VPN). A service in such a start's pod must still be
    proven to share the pod's namespace, but not refused for a kill switch the VPN never has - else every start with
    --service and that VPN failed after its pod was made."""
    add_redis()
    a = start("--service", "cache:image=redis,port=6379")
    sv.check(a, s)
    a.vpn = "corp"
    a.vpn_profile = {"type": "tailscale", "name": "corp", "tailscale": {}}
    a.control_dir = tmp_path / "run"
    a.control_dir.mkdir()
    (a.control_dir / s.RUNTIME.SERVICES_GO).touch()
    run, calls = pod_podman(network, True, killswitch=False)
    answer = sv.launch(a, "agent-x", tmp_path, lambda: True, s, run, out=lambda _: None, readlink=same_userns)
    assert answer["ok"] is ok, answer
    assert [c for c in calls if c[1:3] == ["container", "inspect"]], "the namespace is proven all the same"
    if not ok:
        assert ["podman", "rm", "--force", "--time", "0", "agent-x-service-cache"] in calls
    # With an exit node the kill switch is there and must be in place.
    a.vpn_profile = {"type": "tailscale", "name": "corp", "tailscale": {"exit_node": "exit"}}
    (a.control_dir / s.RUNTIME.SERVICES_STARTED).unlink()
    run, calls = pod_podman(f"container:{INFRA}", True, killswitch=False)
    assert sv.launch(a, "agent-x", tmp_path, lambda: True, s, run, out=lambda _: None,
                     readlink=same_userns)["ok"] is False



# -- the sandbox's own processes in an image that may be a base of yours: Debian's python3, without site --

def test_the_supervisors_python_is_debians_own_without_site(tmp_path):
    """A variant on a base image of yours (python:3.12-bookworm, say) has /usr/local/bin/python3, first on the
    supervisor's PATH, and site-packages whose .pth files and sitecustomize run in every interpreter that processes
    site. The sandbox's own processes there - the supervisor, its guard, the connection proxy, the privacy check, the
    terminals' relay, the host's podman exec on macOS, the resolver of the allowed hosts - run /usr/bin/python3 -I -S:
    the standard library only (which is all they use)."""
    rt = s.RUNTIME
    assert tuple(getattr(rt, "SUPERVISOR_PYTHON", ())) == ("/usr/bin/python3", "-I", "-S")
    assert rt.base_env()["PATH"].startswith("/usr/local/bin:"), "why the path is spelled out"
    a = s.parse_args(["codex", "--workspace", str(tmp_path / "w")])
    _, run = s.pod_commands(a, "pod", tmp_path, tmp_path / "home")
    at = run.index("--entrypoint=/usr/bin/python3")
    assert run[at + 2:at + 5] == ["-I", "-S", "/sandbox/runtime.py"] and run[-1] == "--internal-bootstrap"
    assert s.machine_exec({"pod": "agent-x"}, ["--internal-control-client"])[-4:-1] == \
        ["-I", "-S", "/sandbox/runtime.py"]
    source = (ROOT / "sandbox" / "runtime.py").read_text()
    for entry in ("--internal-guard", "--internal-attach-relay", "--internal-proxy", 'PROXY_DIR / "privacy.py"'):
        line = next(line for line in source.splitlines() if entry in line and ("Popen" in line or "join(" in line
                                                                              or "/sandbox/runtime.py" in line))
        assert "SUPERVISOR_PYTHON" in line, line
    import re as re_
    assert not re_.search(r"""["']python3["'], ["']-I["']""", source), "no python3 found on PATH"
    launcher = (ROOT / "sandbox" / "agent_sandbox.py").read_text()
    assert '"--entrypoint=/usr/bin/python3", a.image, "-I"' not in launcher
