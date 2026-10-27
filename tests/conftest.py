import pytest


def pytest_addoption(parser):
    group = parser.getgroup("agent sandbox integration")
    group.addoption("--agent-live", action="store_true", help="Run real namespace, filesystem and network checks")
    group.addoption("--agent-provider-tests", action="store_true", help="Also make real authenticated Codex/OpenCode requests")
    group.addoption("--agent-backend", choices=["both", "podman", "bwrap"], default="both")
    group.addoption("--agent-image", default="localhost/agent-sandbox:latest")
    group.addoption("--agent-podman-url", default=None)
    group.addoption("--codex-model", default=None)
    group.addoption("--opencode-model", default="openai/gpt-6.1-sol")
    group.addoption("--opencode-codex-auth", action="store_true", help="Explicitly test OpenCode with imported Codex OAuth instead of its own stored auth")
    group.addoption("--free-model", default="opencode/glm-5.3-flash-free", help="Zero-price OpenCode model for practical tool checks")


def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("agent_live") and not config.getoption("--agent-live"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-live"))
        if item.get_closest_marker("agent_provider") and not config.getoption("--agent-provider-tests"):
            item.add_marker(pytest.mark.skip(reason="requires --agent-provider-tests"))


def pytest_configure(config):
    config.addinivalue_line("markers", "agent_live: launches real sandboxes")
    config.addinivalue_line("markers", "agent_provider: makes a real authenticated model request")
