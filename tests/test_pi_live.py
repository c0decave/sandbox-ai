"""Pi (sandbox-ai pi: docs/PI_REVIEW.md) in a real sandbox of its image: it starts against a scripted model on this
host (--llm, no provider: the provider sandbox-ai's extension registers), answers one prompt (pi -p), calls one tool of
sandbox-ai's demo MCP server through the bridge (registered by sandbox-ai's extension), reaches no telemetry host, and
each --pi-enable switch opens exactly its hosts.

Needs Podman and the images localhost/pi-sandbox (sandbox-ai build pi) and localhost/codex-sandbox (sandbox-ai build
codex: the demo MCP server's). Run: python -m pytest -v --agent-live tests/test_pi_live.py (CI: the job "agents-live",
pi; on your machine: scripts/live_check_agents.sh pi). tests/live_agents.py has the checks."""
import pytest

import live_agents as L

AGENT = "pi"
MODULE = L.load_module(AGENT)
SKIP = L.skip_reason([L.image_of(AGENT)])

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]


def test_pi_answers_with_one_mcp_tool_and_no_telemetry(tmp_path):
    L.check_agent_answers_with_one_mcp_tool(tmp_path, AGENT)


@pytest.mark.parametrize("feature,hosts", L.features_to_check(MODULE), ids=[f for f, _ in L.features_to_check(MODULE)])
def test_pi_switch_opens_only_its_hosts(tmp_path, feature, hosts):
    L.check_switch_opens_only_its_hosts(tmp_path, AGENT, feature, hosts)
