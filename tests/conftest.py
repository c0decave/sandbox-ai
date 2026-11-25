import os
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

_REAL_LOGINS = {}


def pytest_addoption(parser):
    group = parser.getgroup("agent sandbox integration")
    group.addoption("--agent-live", action="store_true", help="Run real namespace, filesystem and network checks")
    group.addoption("--agent-provider-tests", action="store_true", help="Also make real authenticated Codex/OpenCode requests")
    group.addoption("--agent-network-tests", action="store_true", help="Also run live tests that download from the internet (e.g. Playwright browsers)")
    group.addoption("--agent-backend", choices=["both", "podman", "bwrap"], default="both")
    group.addoption("--agent-image", default=None, help="Override agent-specific default images")
    group.addoption("--agent-podman-url", default=None)
    group.addoption("--codex-model", default=None)
    group.addoption("--claude-model", default="sonnet")
    group.addoption("--opencode-model", default="openai/gpt-6.1-sol")
    group.addoption("--opencode-codex-auth", action="store_true", help="Explicitly test OpenCode with imported Codex OAuth instead of its own stored auth")
    group.addoption("--local-llm", default=None, metavar="KIND:IP:PORT",
                    help="Live-test OpenCode against a local server, e.g. ollama:192.168.1.20:11434 (lmstudio, vllm)")
    group.addoption("--free-model", default="opencode/glm-5.3-flash-free", help="Zero-price OpenCode model for practical tool checks")
    group.addoption("--live-vpn-profile", default=None, metavar="NAME",
                    help="A VPN profile (sandbox-ai vpn add) for the live VPN checks of test_live_check.py")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("agent_live") and not config.getoption("--agent-live"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-live"))
        if item.get_closest_marker("agent_provider") and not config.getoption("--agent-provider-tests"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-provider-tests"))
        if item.get_closest_marker("agent_network") and not config.getoption("--agent-network-tests"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-network-tests"))


def pytest_configure(config):
    """Before any test module is imported: the real logins are fingerprinted and HOME is moved to a temporary
    directory for the session (home_guard), so no test can write over them. Live tests keep the real HOME (their
    sandboxes need the real Podman storage and logins); their own fixtures keep them apart."""
    if config.getoption("--agent-live"):
        os.environ.setdefault(home_guard.MARKER, "live")
    else:
        _REAL_LOGINS.update(home_guard.fingerprint(home_guard.login_files()))
        home_guard.ensure()
        # Builds with a fake podman: no check of names first (its own tests in test_images.py turn it on).
        os.environ.setdefault("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
        # Starts with --llm ask no server for its context window (test_llm.py turns it on for its fake servers).
        os.environ.setdefault("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")
        # A start's check of its MCP servers asks Podman for no image (the tests' images are made up; the tests of
        # that check give it their own answer).
        os.environ.setdefault("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
        # A start builds no missing image of its own (the tests that check it turn it on).
        os.environ.setdefault("SANDBOX_AI_NO_AUTO_BUILD", "1")
        # Secrets kept in private files by the tests need no primary password (plain files, the choice a machine
        # without anyone to ask makes; test_secrets_primary_password.py turns it off).
        os.environ.setdefault("SANDBOX_AI_SECRETS_PLAIN", "1")
        # Podman's --init has its init binary (catatonit) on the tests' hosts, whatever this machine has (the tests
        # of a host without it say so: SANDBOX_AI_INIT_BINARY=none; test_hostinit.py).
        os.environ.setdefault("SANDBOX_AI_INIT_BINARY", "/usr/libexec/podman/catatonit")
        # A start compacts no logs in the background (test_statedirs.py turns it on).
        os.environ.setdefault("SANDBOX_AI_AUTO_COMPACT", "0")
        # A start tells its agent nothing about its sandbox (no INTRO.md, no line in the agent's instructions): the
        # tests compare each harness's own configuration exactly. The intro's tests turn it on for every harness
        # (test_intro.py, test_resources_reach_agent.py); a user's start has it on.
        os.environ.setdefault("SANDBOX_AI_INTRO", "0")
    config.addinivalue_line("markers", "agent_live: launches real sandboxes")
    config.addinivalue_line("markers", "agent_provider: makes a real authenticated model request")
    config.addinivalue_line("markers", "agent_network: downloads from the internet inside the sandbox")


@pytest.fixture(autouse=True)
def neutral_working_directory(tmp_path_factory, monkeypatch):
    """The default workspace is the current directory; the checkout itself is refused as a workspace
    (the agent could rewrite the launcher), so tests start from an empty directory."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def private_runtime_state(request, tmp_path_factory, monkeypatch):
    """Run registrations (sandbox-ai net) and default log directories of launches under test stay in
    temporary directories, never in the HOME of whoever runs the tests. Live tests keep the real
    XDG_RUNTIME_DIR: rootless Podman and systemd-run --user find the user's session through it."""
    if request.node.get_closest_marker("agent_live") is None:
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("xdg-runtime")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))  # e.g. hosts list's status


ANTHROPIC_DOMAINS = ("anthropic.com", "claude.ai", "claude.com")


@pytest.fixture(autouse=True)
def no_requests_to_anthropic(monkeypatch):
    """No test asks Anthropic anything in this process (the launcher's account check of a Claude login asks
    api.anthropic.com with the login's access token): such a request fails as a network failure would, without a
    connection. Tests fake the lookup (claude_profile_account) or serve it on 127.0.0.1."""
    import urllib.error
    import urllib.parse
    import urllib.request
    real = urllib.request.OpenerDirector.open

    def guarded(self, fullurl, *args, **kwargs):
        url = fullurl if isinstance(fullurl, str) else fullurl.full_url
        host = (urllib.parse.urlsplit(url).hostname or "").lower().rstrip(".")
        if any(host == domain or host.endswith("." + domain) for domain in ANTHROPIC_DOMAINS):
            raise urllib.error.URLError("tests never contact Anthropic")
        return real(self, fullurl, *args, **kwargs)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded)


@pytest.fixture(autouse=True)
def writable_tmp_path(request):
    """After each test, every directory under its tmp_path is writable again (a test may leave a read-only tree,
    as an installed MCP rootfs is). Otherwise pytest cannot remove old base directories, keeps each run's whole
    basetemp as garbage-*, and /tmp - a tmpfs in a sandbox, so memory - fills up run by run."""
    if "tmp_path" not in request.fixturenames:
        yield
        return
    import os
    root = request.getfixturevalue("tmp_path")  # the test uses it anyway; taken now, while it exists
    yield
    for directory, dirs, _ in os.walk(root):
        for name in dirs:
            child = os.path.join(directory, name)
            if not os.path.islink(child):
                try:
                    os.chmod(child, os.lstat(child).st_mode | 0o700)
                except OSError:
                    pass


# The folders an untrusted command (runtime.py --untrusted) may change; kept in step with UNTRUSTED_WRITABLE there
# (test_security_review_untrusted.py checks the tuple).
UNTRUSTED_WRITABLE = ("/work", "/workspaces", "/tmp", "/var/tmp", "/dev", "/proc")


@pytest.fixture
def untrusted_place(tmp_path):
    """A folder for a made-up HOME of an untrusted command, outside the folders such a command may change: tmp_path
    where it lies outside them (a local run with TMPDIR under /root), else a fresh folder below the real user's
    ~/.cache (GitHub's runner: pytest's tmp is under /tmp, and a HOME there is refused - /tmp is the command's to
    change, so the agents' folders below it would be too; inside a sandbox HOME is /root, on a host /home/USER).
    Removed afterwards."""
    import pwd
    import shutil
    import tempfile

    def changeable(path):
        real = os.path.realpath(path)
        return any(real == item or real.startswith(item + "/") for item in UNTRUSTED_WRITABLE)
    if not changeable(tmp_path):
        yield tmp_path
        return
    cache = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
    if changeable(cache):
        pytest.skip(f"no folder outside {', '.join(UNTRUSTED_WRITABLE)} to make a HOME in (tmp_path {tmp_path}, the "
                    f"user's {cache})")
    cache.mkdir(parents=True, exist_ok=True)
    made = Path(tempfile.mkdtemp(prefix="sandbox-ai-untrusted-test-", dir=cache))
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


@pytest.fixture(autouse=True)
def no_fake_host_process_outlives_the_test():
    """Nothing of a fake host (live_fakes.Host) a test made runs on after it: what is left (an MCP server's relay
    outlives its start by design, a session's supervisor, ...) is ended by its exact PID and fails the test - it
    stops what it started (`sandbox-ai mcp stop NAME`). 2026-11-20: four processes of every run of one test lived for
    hours. Free for the tests that make no host (live_fakes not even imported)."""
    fakes = sys.modules.get("live_fakes")
    before = len(fakes.HOSTS) if fakes is not None else 0
    yield
    fakes = sys.modules.get("live_fakes")
    problems = fakes.guard_since(before) if fakes is not None else []
    if problems:
        pytest.fail("the test left processes of its fake host running (ended now; stop what the test started, e.g. "
                    "`sandbox-ai mcp stop NAME`):\n" + "\n".join(problems), pytrace=False)


def pytest_sessionfinish(session, exitstatus):
    """A run that changed a real login file fails, whatever else passed: the next request of an agent running these
    tests would carry a fixture token."""
    touched = home_guard.changed(_REAL_LOGINS) if _REAL_LOGINS else []
    if touched:
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        message = ("tests changed the real login files: " + ", ".join(touched) + " (a test wrote into the real HOME; "
                   "only an agent refreshing its own login during the run would be harmless)")
        if reporter is not None:
            reporter.write_line(message, red=True, bold=True)
        else:
            print(message, file=sys.stderr)
        session.exitstatus = 1
