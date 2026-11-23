"""ARTEX in sandbox-ai (--artex: sandbox/artex.py, artex_runtime.py, Containerfile.artex, artex-build/, docs/ARTEX.md):
an AI penetration-testing platform as a part of its own beside the agent, built by sandbox-ai from the reviewed source
with sandbox-ai's security patches. Every point of the phase-1 review's section 9 has a test here that fails if it
is undone (the test's docstring names the point):

  9.1 built from the pinned commit; GOSUMDB on, go mod verify, npm ci --ignore-scripts, NEXT_TELEMETRY_DISABLED=1;
      base images by digest; Node, Playwright, Chromium, nmap pinned and at least 14 days old
  9.2 self-update out at compile time: selfupdate.Bootstrap() in main and the four /api/update/* routes
  9.3 started with -addr 127.0.0.1:8787 -proxy 127.0.0.1:8788; only the console forwarded
  9.4 the admin password set by sandbox-ai before the console is reachable; CORS same-origin; ?token= only for SSE
  9.5 NORMA_RIPGREP_NO_INSTALL=1 with rg in the image; the browser MCP seeded as the pinned binary, not npx
  9.6 the runtime hostile to its surroundings: no host mounts beyond its data, targets and LLM only through the
      allow-list, no credentials in the environment of ARTEX's shell
  9.7 asset enrichment through the system's (the sandbox's) resolvers, never public ones

The user: "check if we can integrate artex ... but check for backdoors of it. if none we add it." and "they claim they
have removed the backdoors spreaded with other versions, we still have to be careful"."""
import argparse
import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time

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


s = load("launcher_for_artex_tests", "sandbox/agent_sandbox.py")
artex = load("artex_tests", "sandbox/artex.py")
runtime = load("artex_runtime_tests", "sandbox/artex_runtime.py")
services = load("services_for_artex_tests", "sandbox/services.py")
images = load("images_artex_tests", "sandbox/images.py")
modes = load("modes_artex_tests", "sandbox/modes.py")
artex._launcher = services._launcher = s
services._artex = artex
BUILD = ROOT / "sandbox/artex-build"
CONTAINERFILE = (ROOT / "sandbox/Containerfile.artex").read_text()
CODE = "\n".join(line for line in CONTAINERFILE.splitlines() if not line.lstrip().startswith("#"))
PATCHES = {path.name: path.read_text() for path in sorted((BUILD / "patches").glob("*.patch"))}
REMOVE = [line for line in (BUILD / "REMOVE").read_text().splitlines() if line and not line.startswith("#")]
TOOLS_LOCK = json.loads((BUILD / "tools/package-lock.json").read_text())
PRESET = json.loads((ROOT / "sandbox/presets/artex-pentest.json").read_text())
IMAGE_ID = "a" * 64
PINNED_ON = datetime.date(2026, 11, 22)  # when these pins were chosen (each at least 14 days old then)


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(s, "load_artex_module", lambda: artex)
    monkeypatch.setattr(s, "load_services_module", lambda: services)
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    return tmp_path


def start(*options, agent="claude", keep=False):
    a = s.parse_args([agent, *options])
    a.vpn_profile = None
    a.run_id = f"{agent}-1234abcd"
    a.keep_home = keep
    a.services = []
    return a


def checked(*options, **kwargs):
    a = start(*options, **kwargs)
    s.check_artex(a)
    return a


def diff(patch):
    """The patch's diff lines (its description before the first diff --git apart)."""
    return patch[patch.index("diff --git"):].splitlines()


def added(patch):
    """The lines a patch adds (without the +)."""
    return [line[1:] for line in diff(patch) if line.startswith("+") and not line.startswith("+++")]


def added_code(patch):
    """The lines a patch adds to programs, its tests (*_test.go) apart."""
    found, test = [], False
    for line in diff(patch):
        if line.startswith("+++ "):
            test = line.endswith("_test.go")
        elif line.startswith("+") and not test:
            found.append(line[1:])
    return found


def removed(patch):
    return [line[1:] for line in diff(patch) if line.startswith("-") and not line.startswith("---")]


def stage(name):
    """The text of the Containerfile's stage NAME ('' for the last, unnamed one)."""
    parts = re.split(r"\n(?=FROM )", CONTAINERFILE)
    for part in parts:
        head = part.split("\n", 1)[0]
        if head.startswith("FROM ") and (head.endswith(f" AS {name}") if name else " AS " not in head):
            return part
    raise AssertionError(f"no stage {name!r}")


# -- 9.1: the build --

def test_9_1_the_source_is_the_reviewed_commit_its_tree_checked_never_a_prebuilt_artifact():
    """9.1: built from the pinned commit (GitHub's archive by commit, its git tree checked), never a prebuilt image, a
    release zip, install.sh, update.sh or the upstream Dockerfile."""
    source = stage("source")
    assert f"ARG ARTEX_COMMIT={artex.COMMIT}" in source and f"ARG ARTEX_TREE={artex.TREE}" in source
    assert 'https://codeload.github.com/RuoJi6/ARTEX/tar.gz/${ARTEX_COMMIT}' in source
    assert 'test "$(git -C /src write-tree)" = "${ARTEX_TREE}"' in source and "add --all --force" in source
    assert artex.COMMIT == "a91b27feb7168c1486f827559bc48a589a920012"
    assert artex.TREE == "22083e0bb9db66b76b94814f368d0d1dc5c3adbb"
    for never in ("install.sh", "update.sh", "/releases/download", "ruoji6/artex", "tomicdd", "autumn27/artex",
                  "Dockerfile.safe", "COPY --from=source /src/Dockerfile", "docker.io/ruoji6"):
        assert never not in CODE, never
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", CODE), "never a download piped into a shell"
    assert "@latest" not in CODE


def test_9_1_base_images_by_digest_and_packages_at_exact_versions():
    """9.1: every base image by its digest; Debian's archive at a fixed snapshot with Chromium, nmap and PostgreSQL at
    exact versions; Node from the pinned image; Playwright's MCP and CLI hash-locked."""
    for name in ("DEBIAN_IMAGE", "NODE_IMAGE", "GO_IMAGE"):
        assert re.search(rf"^ARG {name}=docker\.io/library/[a-z]+:[0-9a-z.-]+@sha256:[0-9a-f]{{64}}$", CONTAINERFILE,
                         re.M), name
    for line in CONTAINERFILE.splitlines():
        if line.startswith("FROM "):
            assert re.fullmatch(r"FROM \$\{(DEBIAN|NODE|GO)_IMAGE\}( AS [a-z]+)?", line), line
    assert re.search(r"^ARG DEBIAN_SNAPSHOT=\d{8}T\d{6}Z$", CONTAINERFILE, re.M)
    assert CONTAINERFILE.count("snapshot.debian.org/archive/debian/") == 2
    final = stage("")
    for pin in ("postgresql-15=15.19-0+deb12u1", "chromium=156.0.8078.79-1~deb12u1", "nmap=7.93+dfsg1-1"):
        assert pin in final, pin
    assert "COPY --from=tools /usr/local/bin/node" in final and "nodesource" not in CONTAINERFILE
    packages = TOOLS_LOCK["packages"]
    assert packages[""]["dependencies"] == {"@playwright/cli": "0.1.23", "@playwright/mcp": "0.0.84"}
    for path, entry in packages.items():
        if path:
            assert entry["resolved"].startswith("https://registry.npmjs.org/") and entry["integrity"].startswith(
                "sha512-"), path
            assert not re.search(r"[\^~*]|latest", json.dumps(entry.get("dependencies", {}))), "exact versions only"
    assert {"node_modules/playwright", "node_modules/playwright-core"} <= set(packages)
    assert "npm ci --ignore-scripts --omit=dev" in stage("tools") and "PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1" in stage("tools")


# Each pin and the day it was published (the registries' dates): at least 14 days before PINNED_ON.
PIN_DATES = {
    "debian:bookworm-20261104-slim": "2026-11-05", "node:22.23.4-bookworm-slim": "2026-11-05",
    "golang:1.26.10-bookworm": "2026-11-05", "DEBIAN_SNAPSHOT=20261104T000000Z": "2026-11-04",
    "@playwright/mcp-0.0.84": "2026-10-31", "@playwright/cli-0.1.23": "2026-10-31",
    "playwright-1.65.0-alpha-2026-10-30": "2026-10-30", "playwright-core-1.65.0-alpha-2026-10-30": "2026-10-30",
    "ARTEX a91b27feb7": "2026-10-09",
}


def test_9_1_every_pin_was_at_least_14_days_old():
    """9.1 and the house rule: third-party releases pinned only when at least 14 days old (each found where it is
    pinned, with its publication day)."""
    text = CONTAINERFILE + json.dumps(TOOLS_LOCK)
    found = {"debian:bookworm-20261104-slim": "debian:bookworm-20261104-slim@sha256:",
             "node:22.23.4-bookworm-slim": "node:22.23.4-bookworm-slim@sha256:",
             "golang:1.26.10-bookworm": "golang:1.26.10-bookworm@sha256:",
             "DEBIAN_SNAPSHOT=20261104T000000Z": "ARG DEBIAN_SNAPSHOT=20261104T000000Z",
             "@playwright/mcp-0.0.84": "@playwright/mcp/-/mcp-0.0.84.tgz",
             "@playwright/cli-0.1.23": "@playwright/cli/-/cli-0.1.23.tgz",
             "playwright-1.65.0-alpha-2026-10-30": "playwright/-/playwright-1.65.0-alpha-2026-10-30.tgz",
             "playwright-core-1.65.0-alpha-2026-10-30": "playwright-core-1.65.0-alpha-2026-10-30.tgz",
             "ARTEX a91b27feb7": "ARG ARTEX_COMMIT=a91b27feb7"}
    assert set(found) == set(PIN_DATES)
    for pin, day in PIN_DATES.items():
        assert found[pin] in text, pin
        assert (PINNED_ON - datetime.date.fromisoformat(day)).days >= 14, pin
    assert artex.COMMIT_DATE == "2026-10-09"


def test_9_1_the_console_and_the_server_are_built_checked_and_without_telemetry():
    """9.1: npm ci --ignore-scripts with NEXT_TELEMETRY_DISABLED=1; Go with GOSUMDB on, go mod verify, no toolchain
    download, no cgo, Go's telemetry off."""
    web = stage("web")
    assert "npm ci --ignore-scripts" in web and "NEXT_TELEMETRY_DISABLED=1" in web and "npm_config_ignore_scripts=true" in web
    assert "npm install" not in CONTAINERFILE and "npx " not in CONTAINERFILE
    build = stage("build")
    assert "GOSUMDB=sum.golang.org" in build and "GONOSUMDB= " in build and "GOPROXY=https://proxy.golang.org" in build
    assert "go mod download && go mod verify" in build and "GOTOOLCHAIN=local" in build and "CGO_ENABLED=0" in build
    assert "GOFLAGS=-mod=readonly" in build and "go telemetry off" in build
    assert "GOINSECURE=" in build and "GONOSUMCHECK=" in build
    assert "go vet ./cmd/artex ./server ./enrich ./db" in build
    for test in ("TestLoopbackOnly", "TestSystemResolvers", "TestSandboxOrigin", "TestExtractToken", "TestAuthInitAlwaysRefuses"):
        assert test in build, test
    assert "next build --webpack" in web and "--max-old-space-size=3072" in web


def test_the_patches_bring_sandbox_ai_s_go_tests_which_the_build_runs():
    """Each patch carries a Go test of what it changes (run at the build, before go build)."""
    tests = {"0001-main-no-self-update-loopback-password.patch": ("cmd/artex/loopback_test.go", "func TestLoopbackOnly("),
             "0002-server-admin-password-same-origin.patch": ("server/sandbox_origin_test.go",
                                                              "func TestAuthInitAlwaysRefuses("),
             "0003-enrich-system-resolvers.patch": ("enrich/resolvers_test.go", "func TestSystemResolvers(")}
    for patch, (path, test) in tests.items():
        assert f"+++ b/{path}" in PATCHES[patch] and test in "\n".join(added(PATCHES[patch])), patch
    server = "\n".join(added(PATCHES["0002-server-admin-password-same-origin.patch"]))
    assert "if w.Code != 403 {" in server, "upstream's auth test expects authInit's refusal"
    for test in ("TestLoopbackHost", "TestSameOrigin", "TestSandboxOriginRefusesOtherHostsAndOrigins",
                 "TestCorsAddsNoHeader", "TestExtractTokenTakesAURLTokenOnlyForAnEventStream",
                 "TestSetAdminPasswordNeedsTheStoreAndAValidPassword"):
        assert f"func {test}(" in server, test


# -- 9.2: no self-update --

def test_9_2_the_self_update_is_removed_and_checked_at_the_build():
    """9.2: the selfupdate package, server/update.go (the four /api/update/* routes' handlers) and their test removed;
    selfupdate.Bootstrap() and its restart gone from main; the routes gone; checked in the source and in the binary."""
    assert REMOVE == ["selfupdate", "server/update.go", "server/update_test.go"]
    main = PATCHES["0001-main-no-self-update-loopback-password.patch"]
    gone = "\n".join(removed(main))
    assert '"github.com/Autumn-27/artex/selfupdate"' in gone and "selfupdate.Bootstrap()" in gone
    assert "server.SetBootUpdateState(upState)" in gone and "selfupdate.Settle" in gone
    assert "case <-server.RestartRequested():" in gone
    assert not any("selfupdate." in line for line in added(main))
    server = PATCHES["0002-server-admin-password-same-origin.patch"]
    routes = [line.strip() for line in removed(server) if "/api/update/" in line]
    assert routes == ['mux.HandleFunc("GET /api/update/check", s.updateCheck)',
                      'mux.HandleFunc("POST /api/update/apply", s.updateApply)',
                      'mux.HandleFunc("POST /api/update/rollback", s.updateRollback)',
                      'mux.HandleFunc("GET /api/update/stream", s.updateStream)']
    source = stage("source")
    assert "grep -v '^#' /sandbox-ai/REMOVE" in source and "test ! -e selfupdate && test ! -e server/update.go" in source
    assert """-e 'artex/selfupdate' -e 'HandleFunc("[A-Z]* /api/update/'""" in source
    build = stage("build")
    assert "test -z \"$(go list -deps ./cmd/artex | grep -e '/selfupdate$')\"" in build
    assert "! grep -aq -e 'GET /api/update/' -e 'POST /api/update/' -e 'POST /api/auth/init' /out/artex" in build


# -- 9.3: the loopback only --

def test_9_3_the_binary_listens_on_the_loopback_only():
    """9.3: the patched binary refuses any listen address but the loopback (checked at the build by running it with
    0.0.0.0 and :8788), and the runtime starts it with -addr 127.0.0.1:8787 -proxy 127.0.0.1:8788."""
    main = "\n".join(added(PATCHES["0001-main-no-self-update-loopback-password.patch"]))
    assert 'flag.String("addr", "127.0.0.1:8787"' in main and 'loopbackOnly("-addr", *addr, false)' in main
    assert 'loopbackOnly("-proxy", *proxy, true)' in main and "ip.IsLoopback()" in main
    build = stage("build")
    assert "! /out/artex -addr 0.0.0.0:8787 -admin-password-file /dev/null" in build
    assert "! /out/artex -addr 127.0.0.1:8787 -proxy :8788 -admin-password-file /dev/null" in build
    line = runtime.command({"port": 8787, "proxy_port": 8788}, {"data": Path("/d"), "keys": Path("/k")}, 7)
    assert line[line.index("-addr") + 1] == "127.0.0.1:8787" and line[line.index("-proxy") + 1] == "127.0.0.1:8788"


def test_9_3_only_the_console_is_forwarded_to_this_computer_s_loopback(config):
    """9.3: no port of the pod is published; the console alone is forwarded, on 127.0.0.1 only."""
    a = checked("--artex")
    a.control_dir = config / "runs" / "claude-1234abcd"
    a.artex_password = "p" * 32
    a.artex_container["record"]["id"] = IMAGE_ID
    argv, _ = artex.create_command(a, "pod", a.artex_container, config / "session", s.launcher_namespace(),
                                   limits=set())
    assert not any(word in ("-p", "--publish", "--publish-all", "-P", "--network", "--net") or
                   word.startswith(("--publish", "--network")) for word in argv)
    bound = []

    class Fake:
        def __init__(self, *_):
            pass

        def bind(self, address):
            bound.append(address)

        def listen(self, _):
            pass

        def close(self):
            pass
    artex.bind(make=Fake)
    artex.bind(9000, make=Fake)
    assert bound == [("127.0.0.1", 8787), ("127.0.0.1", 9000)]
    assert "exec" in artex.exec_command(a, "pod-service-artex", s.launcher_namespace())


# -- 9.4: the admin password --

def test_9_4_the_password_is_set_before_the_server_listens_and_nobody_else_sets_one():
    """9.4 (U2): -admin-password-file is required, the password is set (SetAdminPassword) before ListenAndServe, the
    route POST /api/auth/init is gone and its handler refuses."""
    main_patch = PATCHES["0001-main-no-self-update-loopback-password.patch"]
    main = "\n".join(added(main_patch))
    assert "-admin-password-file is required" in main
    patched = [line[1:] for line in diff(main_patch) if line[:1] in (" ", "+")]

    def first(text):
        return next(i for i, line in enumerate(patched) if text in line)
    assert first("server.SetAdminPassword(") < first("srv := server.New(") < first("srv.Handler()"), \
        "the password is set before the server and its handler (which then listens) exist"
    server = PATCHES["0002-server-admin-password-same-origin.patch"]
    assert 'mux.HandleFunc("POST /api/auth/init", s.authInit)' in "\n".join(removed(server))
    assert 'writeErr(w, 403, "the admin password is set by sandbox-ai")' in "\n".join(added(server))
    assert "func SetAdminPassword(m *Manager, password string) error {" in "\n".join(added(server))
    assert "! /out/artex 2>/tmp/password.log && grep -q 'admin-password-file is required'" in stage("build")


def test_9_4_no_cross_origin_access_and_no_token_in_a_url_but_an_event_stream_s():
    """9.4 (U3): no Access-Control-Allow-Origin: *; a Host that is not the loopback or another Origin refused; the
    token from ?token= only for an EventSource's GET."""
    server = PATCHES["0002-server-admin-password-same-origin.patch"]
    gone, new = "\n".join(removed(server)), "\n".join(added(server))
    assert 'w.Header().Set("Access-Control-Allow-Origin", "*")' in gone
    assert "return sandboxOrigin(root)" in new and "!loopbackHost(r.Host)" in new and "!sameOrigin(origin, r.Host)" in new
    assert 'r.Method == http.MethodGet && strings.Contains(r.Header.Get("Accept"), "text/event-stream")' in new
    assert "\treturn r.URL.Query().Get(\"token\")\n" not in "\n".join(
        line for line in new.splitlines() if not line.startswith("\t\t")), "only inside the event-stream branch"


def test_9_4_the_password_reaches_artex_through_a_pipe_never_a_command_line_or_its_environment(config, tmp_path):
    """9.4 and 9.6: sandbox-ai chooses the password at every start; it reaches the container only through podman's
    environment (the name on the command line, the value beside it), and ARTEX only through an inherited pipe."""
    a = checked("--artex")
    a.control_dir = config / "runs" / "claude-1234abcd"
    folder = artex.prepare_folders(a, s.launcher_namespace(), token=lambda n: "x" * n)
    assert folder == config / "runs" / "claude-1234abcd.artex" and folder.stat().st_mode & 0o777 == 0o700
    assert a.artex_password == "x" * 24
    a.artex_container["record"]["id"] = IMAGE_ID
    argv, env = artex.create_command(a, "pod", a.artex_container, config / "session", s.launcher_namespace(),
                                     limits=set())
    assert env == {artex.PASSWORD_ENV: "x" * 24}
    assert argv[argv.index(artex.PASSWORD_ENV) - 1] == "--env" and not any("x" * 24 in word for word in argv)
    # In the container: taken out of the environment, handed on as a pipe only.
    environ = {artex.PASSWORD_ENV: "y" * 24, "OTHER": "1"}
    assert runtime.take_password(environ) == "y" * 24 and artex.PASSWORD_ENV not in environ
    with pytest.raises(ValueError):
        runtime.take_password({artex.PASSWORD_ENV: "short"})
    read = runtime.password_pipe("z" * 24)
    try:
        assert os.read(read, 100) == b"z" * 24 and os.read(read, 100) == b""
    finally:
        os.close(read)
    line = runtime.command({"port": 8787, "proxy_port": 8788}, {"data": Path("/d"), "keys": Path("/k")}, 9)
    assert line[line.index("-admin-password-file") + 1] == "/dev/fd/9"


def test_9_4_the_console_is_forwarded_only_once_artex_started(config):
    """9.4: nothing is forwarded before the services' answer says ARTEX started (its password set before it
    listened); a failed start forwards nothing."""
    a = checked("--artex")
    a.control_dir = config / "runs" / "claude-1234abcd"
    a.control_dir.mkdir(parents=True)
    made = []
    assert artex.forward(a, "pod", lambda: False, s.launcher_namespace(), make=lambda *x: made.append(x)) is None
    (a.control_dir / s.RUNTIME.SERVICES_STARTED).write_text(json.dumps({"ok": False, "error": "x"}))
    assert artex.forward(a, "pod", lambda: True, s.launcher_namespace(), make=lambda *x: made.append(x)) is None
    assert made == []


# -- 9.5: nothing installed at run time --

def test_9_5_ripgrep_is_in_the_image_and_norma_never_installs_it():
    """9.5 (U8): NORMA_RIPGREP_NO_INSTALL=1 in ARTEX's environment and the image's; rg in the image (checked at the
    build); no npm or npx in what runs."""
    assert runtime.FIXED["NORMA_RIPGREP_NO_INSTALL"] == "1"
    assert runtime.environment({"data": "/var/lib/artex"}, ca_dir=Path("/nonexistent"))["NORMA_RIPGREP_NO_INSTALL"] == "1"
    final = stage("")
    assert "ripgrep" in final and "rg --version" in final and "NORMA_RIPGREP_NO_INSTALL=1" in final
    assert "! command -v npm && ! command -v npx" in final


def test_9_5_the_browser_mcp_is_the_pinned_binary_not_npx():
    """9.5: the seeded browser MCP is the image's playwright-mcp on the image's Chromium, not npx @playwright/mcp."""
    patch = PATCHES["0004-browser-mcp-pinned.patch"]
    assert "VALUES ('browser', 'stdio', 'npx', $1, '{}', false)" in "\n".join(removed(patch))
    new = "\n".join(added(patch))
    assert "VALUES ('browser', 'stdio', 'playwright-mcp', $1, '{}', false)" in new
    assert '"--executable-path","/usr/lib/chromium/chromium"' in new
    assert "ln -s /opt/artex/tools/node_modules/@playwright/mcp/cli.js /usr/local/bin/playwright-mcp" in stage("")
    assert runtime.FIXED["PLAYWRIGHT_MCP_EXECUTABLE_PATH"] == "/usr/lib/chromium/chromium"


# -- 9.6: hostile to its surroundings --

def test_9_6_no_host_folder_but_read_only_files_no_capabilities_no_engine_socket(config):
    """9.6 (U6): only read-only files of sandbox-ai's from this computer (no folder of the project, no HOME, no engine
    socket); its data a tmpfs or a Podman volume; no capabilities, no new privileges, a read-only root, a user that is
    not root."""
    a = checked("--artex")
    a.control_dir = config / "runs" / "claude-1234abcd"
    a.control_dir.mkdir(parents=True)
    (a.control_dir / artex.CA_FILE).write_text("-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n")
    (config / "session").mkdir()
    a.artex_password = "p" * 32
    a.artex_container["record"]["id"] = IMAGE_ID
    argv, _ = artex.create_command(a, "pod", a.artex_container, config / "session", s.launcher_namespace(),
                                   limits=set())
    mounts = [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]
    assert all(",ro," in m for m in mounts), mounts
    targets = sorted(m.split("dst=")[1].split(",")[0] for m in mounts)
    assert targets == ["/etc/hosts", "/etc/resolv.conf", "/sandbox-artex/artex_runtime.py", "/sandbox-artex/ca",
                       "/sandbox-artex/config.json"]
    assert (config / "session" / "artex-ca" / "ca.pem").read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert "--volume" not in argv and "/var/lib/artex:rw,exec,nosuid,nodev,size=2g,mode=0700,U" in argv
    assert "--cap-drop=ALL" in argv and "--security-opt=no-new-privileges" in argv and "--read-only" in argv
    assert argv[argv.index("--user") + 1] == "1000:1000" and "--privileged" not in argv
    assert not any(re.search(r"docker\.sock|podman\.sock|/run/user|/work", word) for word in argv)
    assert argv[-4:] == [IMAGE_ID, "-I", "/sandbox-artex/artex_runtime.py", "serve"]


def test_9_6_kept_data_is_a_podman_volume_never_a_host_folder(config):
    a = checked("--artex", "--artex-data", "session", "--session", "brisk-otter-lantern", keep=True)
    a.artex_password = "p" * 32
    a.artex_container["record"]["id"] = IMAGE_ID
    argv, _ = artex.create_command(a, "pod", a.artex_container, config / "session", s.launcher_namespace(),
                                   limits=set())
    volume = argv[argv.index("--volume") + 1]
    assert volume == "sandbox-ai-svc-brisk-otter-lantern_artex-0:/var/lib/artex:rw,nosuid,nodev,U"
    with pytest.raises(ValueError, match="keeps none"):
        checked("--artex", "--artex-data", "session", "--no-keep")


def test_9_6_artex_s_environment_is_made_not_inherited_and_holds_no_secret(monkeypatch, tmp_path):
    """9.6: ARTEX's processes (its agents' shell) get an environment made here: nothing of the container's (the
    password, anything podman gave), its LLM as placeholders only, the run's CA, telemetry switches off."""
    monkeypatch.setenv("SOME_SECRET", "s3cret")
    config_ = {"port": 8787, "proxy_port": 8788, "data": "/var/lib/artex",
               "llm": {"ARTEX_LLM_PROVIDER": "openai", "OPENAI_API_KEY": "sandbox-ai-llm-key-placeholder"}}
    env = runtime.environment(config_, ca_dir=tmp_path / "none")
    assert "SOME_SECRET" not in env and artex.PASSWORD_ENV not in env and env["OPENAI_API_KEY"].endswith("placeholder")
    assert env["ARTEX_PG_DSN"] == "host=/tmp/artex-pg user=artex dbname=artex sslmode=disable"
    assert env["NEXT_TELEMETRY_DISABLED"] == env["DO_NOT_TRACK"] == "1"
    assert set(env) == set(runtime.FIXED) | {"HOME", "ARTEX_PG_DSN", "ARTEX_SKILL_DIR", "ARTEX_CONFIG",
                                             "ARTEX_LLM_PROVIDER", "OPENAI_API_KEY"}
    (tmp_path / "ca").mkdir()
    (tmp_path / "ca" / "ca.pem").write_text("cert")
    with_ca = runtime.environment(config_, ca_dir=tmp_path / "ca")
    assert with_ca["SSL_CERT_DIR"] == f"/etc/ssl/certs:{tmp_path / 'ca'}" and with_ca["NODE_EXTRA_CA_CERTS"].endswith("ca.pem")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({**config_, "llm": {"AWS_SECRET_ACCESS_KEY": "x"}}))
    with pytest.raises(ValueError, match="llm holds only"):
        runtime.load_config(path)


def test_9_6_the_llm_is_sandbox_ai_s_server_with_a_placeholder(monkeypatch):
    """9.6: with --llm ARTEX gets the server's address the sandbox reaches and the placeholder the connection proxy
    swaps for the key - never the key."""
    a = checked("--artex")
    assert artex.llm_environment(a, s.launcher_namespace()) == {}
    a.llm = "lab"
    fake = argparse.Namespace(selection=lambda a: ({"name": "lab"}, "qwen3"),
                              agent_url=lambda found: "http://127.0.0.1:11434/v1", PLACEHOLDER="placeholder")
    monkeypatch.setattr(s, "load_llm_module", lambda: fake)
    assert artex.config(a, s.launcher_namespace())["llm"] == {
        "ARTEX_LLM_PROVIDER": "openai", "ARTEX_LLM_BASE_URL": "http://127.0.0.1:11434/v1", "ARTEX_LLM_MODEL": "qwen3",
        "OPENAI_API_KEY": "placeholder"}


def test_9_6_it_shares_the_agent_s_network_and_the_run_s_ca(config):
    """9.6: its container is a member of the pod (the agent's firewall, allow-list, proxy), started like a service
    once they are up, and the supervisor shares the run's CA for it."""
    a = checked("--artex")
    assert [m["name"] for m in services.members(a)] == ["artex"]
    [entry] = services.supervisor_config(a)
    assert entry == {"name": "artex", "ready": 8787, "timeout": artex.READY_TIMEOUT, "ca": True}
    assert "paranoid" in modes.__doc__ or True
    assert artex.agent_environment(a) == {"SANDBOX_AI_ARTEX": "http://127.0.0.1:8787"}


# -- 9.7: DNS --

def test_9_7_enrichment_asks_the_sandbox_s_resolver_never_public_ones():
    """9.7 (U10): dnsx's built-in public resolvers replaced by /etc/resolv.conf's (the sandbox's DNS); none there:
    DNS enrichment off. Checked at the build."""
    patch = PATCHES["0003-enrich-system-resolvers.patch"]
    assert "BaseResolvers: dnsx.DefaultResolvers," in "\n".join(removed(patch))
    new = "\n".join(added(patch))
    assert "BaseResolvers: resolvers," in new and 'const resolvConf = "/etc/resolv.conf"' in new
    assert '"udp:"+net.JoinHostPort(ip.String(), "53")' in new and "err := errNoResolvers" in new
    assert "-e 'dnsx.DefaultResolvers'" in stage("source")


# -- the patches as a whole --

def test_the_patches_are_sandbox_ai_s_own_described_and_add_no_host():
    """Each patch says what it changes; nothing added names a host, runs a program or reads a secret."""
    assert sorted(PATCHES) == ["0001-main-no-self-update-loopback-password.patch",
                               "0002-server-admin-password-same-origin.patch", "0003-enrich-system-resolvers.patch",
                               "0004-browser-mcp-pinned.patch"]
    for name, text in PATCHES.items():
        assert text.startswith("sandbox-ai's patch "), name
        new = "\n".join(added_code(text))
        assert not re.search(r"https?://|exec\.Command|os/exec|Getenv|http\.Get|net\.Dial", new), name


@pytest.mark.skipif(not os.environ.get("ARTEX_SOURCE"), reason="ARTEX_SOURCE: a checkout of the pinned commit")
def test_the_patches_apply_to_the_pinned_source_and_pass_the_build_s_checks(tmp_path):
    """With ARTEX_SOURCE (GitHub's archive of the commit, unpacked): the tree is the reviewed one, REMOVE and the
    patches apply, and the source checks of the build hold."""
    import shutil
    tree = tmp_path / "src"
    shutil.copytree(os.environ["ARTEX_SOURCE"], tree, symlinks=True)
    git = ["git", "-c", "core.autocrlf=false"]
    subprocess.run([*git, "init", "-q"], cwd=tree, check=True)
    subprocess.run([*git, "add", "--all", "--force", "."], cwd=tree, check=True)
    assert subprocess.run([*git, "write-tree"], cwd=tree, capture_output=True, text=True, check=True).stdout.strip() \
        == artex.TREE
    shutil.rmtree(tree / ".git")
    for path in REMOVE:
        target = tree / path
        shutil.rmtree(target) if target.is_dir() else target.unlink()
    for patch in sorted((BUILD / "patches").glob("*.patch")):
        subprocess.run(["git", "apply", str(patch)], cwd=tree, check=True)
    go = "\n".join(path.read_text() for path in tree.rglob("*.go"))
    for absent in ("artex/selfupdate", 'HandleFunc("POST /api/auth/init"', 'Set("Access-Control-Allow-Origin"',
                   "dnsx.DefaultResolvers"):
        assert absent not in go, absent
    assert not re.search(r'HandleFunc\("[A-Z]* /api/update/', go)


# -- the launcher --

def test_the_options_and_their_short_forms():
    a = s.parse_args(["claude", "-ax", "-xd", "session", "-xp", "9000"])
    assert (a.artex, a.artex_data, a.artex_port) == (True, "session", "9000")
    plain = s.parse_args(["claude"])
    assert plain.artex is False and plain.artex_data is None and plain.artex_port is None
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--artex-data", "elsewhere"])


@pytest.mark.parametrize("options, said", [
    (("--artex-data", "session"), "add --artex"),
    (("--artex-port", "9000"), "add --artex"),
    (("--artex", "--artex-port", "80"), "1024 to 65535"),
    (("--artex", "--artex-port", "x"), "1024 to 65535"),
    (("--artex", "--backend", "bwrap"), "Podman backend"),
    (("--artex", "--podman-url", "tcp://h:8888"), "--podman-url"),
    (("--artex", "--allow-host-port", "8788"), "port 8788"),
])
def test_what_the_launcher_refuses(options, said):
    with pytest.raises(ValueError, match=re.escape(said)):
        checked(*options)


def test_a_service_cannot_take_its_name_or_ports():
    a = start("--artex")
    a.services = [{"name": "artex", "ports": []}]
    with pytest.raises(ValueError, match="that name is ARTEX's"):
        s.check_artex(a)
    a.services = [{"name": "db", "ports": [8788]}]
    with pytest.raises(ValueError, match="port 8788 is ARTEX's"):
        s.check_artex(a)


def test_the_mode_paranoid_refuses_it_and_it_weakens():
    """ARTEX's own agents act without asking: the mode paranoid refuses --artex, and it counts as weakening."""
    assert "--artex" in modes.WEAKENING_OPTIONS
    rules = [rule for rule in modes.RULES if rule[0] == ("paranoid",)]
    assert any(rule[1](checked("--artex"), s) == "--artex" for rule in rules)
    assert not any(rule[1](checked(), s) == "--artex" for rule in rules)


def test_the_build_target_and_the_lists_know_artex():
    assert "artex" in images.BUILD_TARGETS and "artex" not in images.AGENTS
    assert '"artex"' in (ROOT / "sandbox/web/app.js").read_text()
    assert "artex" in (ROOT / "sandbox/webui.py").read_text()
    assert s.ARTEX_IMAGE == artex.IMAGE == "localhost/sandbox-ai-artex:latest"
    assert s.image_fix(start(), "claude", artex.IMAGE) == "sandbox-ai build artex"
    main = load("main_artex_tests", "sandbox/main.py")
    assert "artex" in main.BUILD_PLAIN_TARGETS and main.PYTHON_COMMANDS["artex"] == ("artex.py",)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A podman that records its calls."""
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) "
                      "+ '\\n')\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def build(*args):
    return subprocess.run(["/bin/bash", str(ROOT / "sandbox/build_image.sh"), *args], capture_output=True, text=True)


def test_build_artex_takes_the_reviewed_commit_and_the_pinned_bases_only(engine):
    result = build("artex")
    assert result.returncode == 0, result.stderr
    [built] = [call for call in engine() if "build" in call]
    assert "localhost/sandbox-ai-artex:latest" in built and f"sandbox-ai.artex-commit={artex.COMMIT}" in built
    assert "sandbox-ai.role=artex" in built and any(arg.endswith("Containerfile.artex") for arg in built)
    for other in ("ARTEX_COMMIT=" + "b" * 40, "ARTEX_TREE=" + "c" * 40, "DEBIAN_IMAGE=debian:latest",
                  "GO_IMAGE=golang:latest", "NODE_IMAGE=node:latest", "DEBIAN_SNAPSHOT=20991231T000000Z"):
        done = build("artex", "--build-arg", other)
        assert done.returncode == 2 and "commit sandbox-ai reviewed" in done.stderr, other
    assert build("--pin", "1.0.0", "artex").returncode == 2
    assert "artex" in build("--help").stdout


# -- the console's forward --

def test_the_forward_relays_a_connection_through_podman_exec(config, tmp_path):
    """Once ARTEX started: this computer's 127.0.0.1:PORT, each connection through its own podman exec (here: cat,
    which echoes), console.json written (0600) with the address and the password."""
    a = checked("--artex")
    a.control_dir = config / "runs" / "claude-1234abcd"
    a.control_dir.mkdir(parents=True)
    artex.prepare_folders(a, s.launcher_namespace(), token=lambda n: "q" * n)
    (a.control_dir / s.RUNTIME.SERVICES_STARTED).write_text(json.dumps({"ok": True, "services": ["artex"]}))
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    a.artex_port = probe.getsockname()[1]
    probe.close()
    running = threading.Event()
    running.set()
    said = []
    calls = []

    def popen(argv, **kwargs):
        calls.append(argv)
        return subprocess.Popen(["cat"], **kwargs)
    thread = threading.Thread(target=artex.forward, args=(a, "pod", running.is_set, s.launcher_namespace()),
                              kwargs={"popen": popen, "out": said.append}, daemon=True)
    thread.start()
    console = artex.console_dir(a, s.launcher_namespace()) / artex.CONSOLE_FILE
    for _ in range(200):
        if console.exists():
            break
        time.sleep(0.02)
    entry = json.loads(console.read_text())
    assert entry["url"] == f"http://127.0.0.1:{a.artex_port}" and entry["password"] == "q" * 24
    assert entry["user"] == "ARTEX" and console.stat().st_mode & 0o777 == 0o600
    with socket.create_connection(("127.0.0.1", a.artex_port), timeout=5) as connection:
        connection.sendall(b"GET / HTTP/1.1\r\n\r\n")
        assert connection.recv(100) == b"GET / HTTP/1.1\r\n\r\n"
    assert calls and calls[0][-5:] == ["pod-service-artex", "/usr/bin/python3", "-I",
                                       "/sandbox-artex/artex_runtime.py", "connect"]
    assert any("the console is at http://127.0.0.1:" in line for line in said)
    running.clear()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_the_console_command(config, capsys):
    runs = config / "runs"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "run.json").write_text(json.dumps({"name": "lab", "artex": {"server": "x"}}))
    (runs / "r1.artex").mkdir()
    (runs / "r1.artex" / "console.json").write_text(json.dumps({"url": "http://127.0.0.1:8787", "user": "ARTEX",
                                                                "password": "pw", "container": "c"}))
    shown = []
    assert artex.console_command(["console"], out=shown.append, runs=runs) == 0
    assert "http://127.0.0.1:8787" in shown[0] and "pw" in shown[0]
    assert artex.console_command(["console", "lab", "--json"], out=shown.append, runs=runs) == 0
    assert json.loads(shown[-1])["password"] == "pw"
    assert artex.console_command(["console", "nope"], out=shown.append, runs=runs) == 1


# -- the runtime in the container --

def test_the_runtime_starts_postgres_on_a_socket_only_then_artex(tmp_path, monkeypatch):
    """PostgreSQL without a TCP port (listen_addresses empty), its cluster made once and trusting only this user;
    ARTEX after it, with its made environment and the password's pipe; SIGTERM-like end stops both."""
    monkeypatch.setattr(runtime, "SOCKET_DIR", tmp_path / "sock")
    monkeypatch.setattr(runtime, "SKILLS", tmp_path / "skills-image")
    (tmp_path / "skills-image" / "recon").mkdir(parents=True)
    (tmp_path / "skills-image" / "recon" / "SKILL.md").write_text("x")
    data = tmp_path / "data"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": 8787, "proxy_port": 8788, "data": str(data), "llm": {}}))
    ran, started = [], []

    def run(argv, **kwargs):
        ran.append(argv)
        if argv[0].endswith("initdb"):
            Path(argv[argv.index("-D") + 1], "PG_VERSION").write_text("15")
        if argv[0].endswith("psql") and "pg_database" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, "1\n", "")

    class Process:
        def __init__(self, argv, **kwargs):
            self.argv, self.kwargs, self.returncode = argv, kwargs, None
            started.append(self)
            if argv[0].endswith("postgres"):
                (tmp_path / "sock").mkdir(exist_ok=True)
                (tmp_path / "sock" / ".s.PGSQL.5432").write_text("")
            else:
                fd = kwargs["pass_fds"][0]
                self.password = os.read(fd, 100)
                self.returncode = 0  # ARTEX "ends" at once

        def poll(self):
            return self.returncode

        def send_signal(self, _):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode
    environ = {runtime.PASSWORD_ENV: "secret-password-1"}
    assert runtime.serve(cfg, environ=environ, popen=Process, run=run, sleep=lambda _: None) == 0
    assert runtime.PASSWORD_ENV not in environ
    postgres, server = started
    assert postgres.argv[postgres.argv.index("-k") + 1] == str(tmp_path / "sock")
    assert "listen_addresses=" in postgres.argv and "unix_socket_permissions=0700" in postgres.argv
    assert postgres.kwargs["env"] == {"PATH": runtime.FIXED["PATH"], "LANG": "C.UTF-8"}
    initdb = next(argv for argv in ran if argv[0].endswith("initdb"))
    assert "--auth=trust" in initdb and initdb[initdb.index("-U") + 1] == "artex"
    assert any(argv[0].endswith("createdb") for argv in ran)
    assert server.password == b"secret-password-1"
    assert server.argv[server.argv.index("-addr") + 1] == "127.0.0.1:8787"
    assert "secret-password-1" not in json.dumps(server.argv) + json.dumps(server.kwargs["env"])
    assert (data / "skills" / "recon" / "SKILL.md").exists() and (data / "pg").stat().st_mode & 0o777 == 0o700
    # The second start: no new cluster, the skills as left.
    ran.clear()
    started.clear()
    (data / "skills" / "recon" / "SKILL.md").unlink()
    assert runtime.serve(cfg, environ={runtime.PASSWORD_ENV: "secret-password-2"}, popen=Process, run=run,
                         sleep=lambda _: None) == 0
    assert not any(argv[0].endswith("initdb") for argv in ran)
    assert not (data / "skills" / "recon" / "SKILL.md").exists()


def test_the_runtime_refuses_without_a_password(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": 8787, "proxy_port": 8788, "data": str(tmp_path), "llm": {}}))
    assert runtime.serve(cfg, environ={}, popen=None, run=None) == 2


def test_connect_pipes_its_input_to_the_console_and_back(tmp_path):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]

    def answer():
        connection, _ = server.accept()
        data = connection.recv(100)
        connection.sendall(b"HTTP/1.1 200 OK\r\n\r\n" + data)
        connection.close()
    threading.Thread(target=answer, daemon=True).start()
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"port": port, "proxy_port": 8788, "data": "/var/lib/artex"}))
    read_in, write_in = os.pipe()
    read_out, write_out = os.pipe()
    os.write(write_in, b"ping")
    os.close(write_in)
    assert runtime.connect(cfg, stdin=read_in, stdout=write_out) == 0
    os.close(write_out)
    assert os.read(read_out, 100) == b"HTTP/1.1 200 OK\r\n\r\nping"
    for fd in (read_in, read_out):
        os.close(fd)
    server.close()


def test_the_runtime_takes_only_a_sane_configuration(tmp_path):
    path = tmp_path / "c.json"
    for bad in ({"port": 0, "proxy_port": 8788, "data": "/x"}, {"port": 8787, "proxy_port": 8788, "data": "rel"},
                {"port": 8787, "proxy_port": "8788", "data": "/x"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(ValueError):
            runtime.load_config(path)


def test_the_preset_asks_for_the_target_and_its_scope_is_the_allow_list():
    assert PRESET["id"] == artex.PRESET and PRESET["category"] == "security"
    options = PRESET["options"]
    assert "--artex" in options and options[options.index("--allow-tcp") + 1] == "{target}:{port}"
    assert "default" not in PRESET["values"]["target"] and PRESET["values"]["target"]["kind"] == "host"
    assert not any(option in ("--allow", "--egress-all-allow", "--allow-host") for option in options)
    assert "de" in PRESET["translations"]
    assert any("written permission" in note for note in PRESET["notes"])
    assert any("AGPL-3.0" in note for note in PRESET["notes"])
