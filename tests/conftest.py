import pytest


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


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("agent_live") and not config.getoption("--agent-live"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-live"))
        if item.get_closest_marker("agent_provider") and not config.getoption("--agent-provider-tests"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-provider-tests"))
        if item.get_closest_marker("agent_network") and not config.getoption("--agent-network-tests"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-network-tests"))


def pytest_configure(config):
    config.addinivalue_line("markers", "agent_live: launches real sandboxes")
    config.addinivalue_line("markers", "agent_provider: makes a real authenticated model request")
    config.addinivalue_line("markers", "agent_network: downloads from the internet inside the sandbox")


@pytest.fixture(autouse=True)
def neutral_working_directory(tmp_path_factory, monkeypatch):
    """The default workspace is the current directory; the checkout itself is refused as a workspace
    (the agent could rewrite the launcher), so tests start from an empty directory."""
    monkeypatch.chdir(tmp_path_factory.mktemp("cwd"))


@pytest.fixture(autouse=True)
def private_runtime_state(tmp_path_factory, monkeypatch):
    """Run registrations (sandbox-ai net) and default log directories of launches under test stay in
    temporary directories, never in the HOME of whoever runs the tests."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path_factory.mktemp("xdg-runtime")))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path_factory.mktemp("xdg-state")))
