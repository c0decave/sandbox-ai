"""Hermes Agent (sandbox-ai hermes: docs/HERMES_REVIEW.md) in a real sandbox of its image: it starts
against a scripted model on this host (--llm, no provider), answers one prompt, calls one tool of sandbox-ai's demo MCP
server through the bridge, reaches no telemetry host, and each --hermes-enable switch opens exactly its hosts.

Needs Podman and the images localhost/hermes-sandbox (sandbox-ai build hermes) and localhost/codex-sandbox (sandbox-ai
build codex: the demo MCP server's). Run: python -m pytest -v --agent-live tests/test_hermes_live.py (CI: the job
"agents-live", hermes; on your machine: scripts/live_check_agents.sh hermes). tests/live_agents.py has the checks."""
import pytest

import live_agents as L

AGENT = "hermes"
MODULE = L.load_module(AGENT)
SKIP = L.skip_reason([L.image_of(AGENT)])

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]


def test_hermes_answers_with_one_mcp_tool_and_no_telemetry(tmp_path):
    L.check_agent_answers_with_one_mcp_tool(tmp_path, AGENT)


@pytest.mark.parametrize("feature,hosts", L.features_to_check(MODULE), ids=[f for f, _ in L.features_to_check(MODULE)])
def test_hermes_switch_opens_only_its_hosts(tmp_path, feature, hosts):
    L.check_switch_opens_only_its_hosts(tmp_path, AGENT, feature, hosts)
