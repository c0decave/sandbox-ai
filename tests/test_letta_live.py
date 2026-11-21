"""Letta Code (sandbox-ai letta: docs/LETTA_REVIEW.md) in a real sandbox of its image: it starts on its local backend
against a scripted model on this host (--llm, no provider), answers one prompt (letta -p), calls one tool of
sandbox-ai's demo MCP server through the bridge (laid into its agent's settings by sandbox-ai's start script), reaches
no telemetry host, and each --letta-enable switch opens exactly its hosts.

Needs Podman and the images localhost/letta-sandbox (sandbox-ai build letta) and localhost/codex-sandbox (sandbox-ai
build codex: the demo MCP server's). Run: python -m pytest -v --agent-live tests/test_letta_live.py (CI: the job
"agents-live", letta; on your machine: scripts/live_check_agents.sh letta). tests/live_agents.py has the checks."""
import pytest

import live_agents as L

AGENT = "letta"
MODULE = L.load_module(AGENT)
SKIP = L.skip_reason([L.image_of(AGENT)])

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]


def test_letta_answers_with_one_mcp_tool_and_no_telemetry(tmp_path):
    L.check_agent_answers_with_one_mcp_tool(tmp_path, AGENT)


@pytest.mark.parametrize("feature,hosts", L.features_to_check(MODULE), ids=[f for f, _ in L.features_to_check(MODULE)])
def test_letta_switch_opens_only_its_hosts(tmp_path, feature, hosts):
    L.check_switch_opens_only_its_hosts(tmp_path, AGENT, feature, hosts)
