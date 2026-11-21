"""OpenCode 2 (sandbox-ai opencode2: docs/OPENCODE2_REVIEW.md) in a real sandbox of its image: it answers one prompt
from a scripted model on this host (--llm, no provider; opencode2 run), calls one tool of sandbox-ai's demo MCP server
through the bridge (its configuration layer's direct tools), reaches no telemetry host, and each --opencode2-enable
switch opens exactly its hosts.

OpenCode 2.2.5's `opencode run` sends its first model call before the MCP servers have connected (the review's "Known
gap"; 2.2.6 waits for them): the tool call is expected to fail on 2.2.5 and to pass once the pin moves.

Needs Podman and the images localhost/opencode2-sandbox (sandbox-ai build opencode2) and localhost/codex-sandbox
(sandbox-ai build codex: the demo MCP server's). Run: python -m pytest -v --agent-live tests/test_opencode2_live.py
(CI: the job "agents-live", opencode2; on your machine: scripts/live_check_agents.sh opencode2). tests/live_agents.py
has the checks."""
import pytest

import live_agents as L

AGENT = "opencode2"
MODULE = L.load_module(AGENT)
SKIP = L.skip_reason([L.image_of(AGENT)])

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]


@pytest.mark.xfail(MODULE.VERSION == "2.2.5", strict=False,
                   reason="OpenCode 2.2.5's opencode run does not wait for MCP servers before its first model call")
def test_opencode2_answers_with_one_mcp_tool_and_no_telemetry(tmp_path):
    L.check_agent_answers_with_one_mcp_tool(tmp_path, AGENT)


@pytest.mark.parametrize("feature,hosts", L.features_to_check(MODULE), ids=[f for f, _ in L.features_to_check(MODULE)])
def test_opencode2_switch_opens_only_its_hosts(tmp_path, feature, hosts):
    L.check_switch_opens_only_its_hosts(tmp_path, AGENT, feature, hosts)
