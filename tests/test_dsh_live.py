"""DeepSeek Harness (sandbox-ai dsh: docs/DSH_REVIEW.md) in a real sandbox of its image: it starts
against a scripted model on this host (--llm, no provider), answers one prompt (dsh headless), calls one tool of
sandbox-ai's demo MCP server through the bridge and reaches no telemetry host; it has no switches, so a start opens no
host of its own and its telemetry host never resolves.

Needs Podman and the images localhost/dsh-sandbox (sandbox-ai build dsh) and localhost/codex-sandbox (sandbox-ai build
codex: the demo MCP server's). Run: python -m pytest -v --agent-live tests/test_dsh_live.py (CI: the job "agents-live",
dsh; on your machine: scripts/live_check_agents.sh dsh). tests/live_agents.py has the checks."""
import pytest

import live_agents as L

AGENT = "dsh"
SKIP = L.skip_reason([L.image_of(AGENT)])

pytestmark = [pytest.mark.agent_live, pytest.mark.skipif(SKIP is not None, reason=SKIP or "")]


def test_dsh_answers_with_one_mcp_tool_and_no_telemetry(tmp_path):
    L.check_agent_answers_with_one_mcp_tool(tmp_path, AGENT)


def test_dsh_opens_nothing_of_its_own(tmp_path):
    L.check_no_switch_opens_nothing(tmp_path, AGENT)
