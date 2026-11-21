"""Codex's MCP results start with a header of its own - "Wall time: 0.0057 seconds\nOutput:" - and then the server's
answer, `{"error": "path: ... does not exist"}` (research round 3, g9_fail_guess__codex__laguna__hint1: 120 guessed
mcp__files__read_file calls, every one failed, and the loop guard's failures layer never noticed: runtime.llm_tool_failed
reads a JSON object only at the start of the result). The header is now skipped before the error check; a command's
output (exec_command: "Chunk ID ... Process exited with code 0 ... Output:") is not read as an MCP answer."""
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_llm_loop_live as live  # noqa: E402

rt, llm = live.rt, live.llm

PARTS = [{"type": "input_text", "text": "Wall time: 0.0057 seconds\nOutput:"},
         {"type": "input_text", "text": "{\n  \"error\": \"path: /w/notes/a.txt does not exist\"\n}"}]


def test_unit_codex_mcp_error_shapes_are_failures():
    assert rt.llm_tool_failed(PARTS)
    assert rt.llm_tool_failed("Wall time: 0.0057 seconds\nOutput:\n{\"error\": \"no such path\"}")
    assert rt.llm_tool_failed("Wall time: 1.2 seconds\nOutput:\n{\"isError\": true}")
    # the server's answer that worked, and a command's output that prints such JSON: no failure
    assert not rt.llm_tool_failed([PARTS[0], {"type": "input_text", "text": "{\"path\": \"/w/a.txt\", \"text\": \"x\"}"}])
    assert not rt.llm_tool_failed("Chunk ID: 1a2b\nWall time: 0.1 seconds\nProcess exited with code 0\nOutput:\n"
                                  "{\"error\": \"a field in the file\"}")


def codex_guessing(count):
    calls = [("mcp__files__read_file", {"path": f"notes/guess{i}.txt"},
              [PARTS[0], {"type": "input_text", "text": f"{{\"error\": \"path: notes/guess{i}.txt does not exist\"}}"}],
              None) for i in range(count)]
    return live.conversation_of("responses", calls, [live.shell_tool("responses", "mcp__files__read_file", ("path",),
                                                                     ["path"])])


def test_regression_codexs_failed_mcp_calls_get_the_failures_note():
    document = codex_guessing(6)
    done = rt.llm_request(live.entry("lmstudio"), "responses", document)
    assert (done["loop"]["kind"], done["loop"]["tool"], done["loop"]["count"]) == ("failures", "mcp__files__read_file", 6)
    outputs = [item["output"] for item in document["input"] if item.get("type") == "function_call_output"]
    assert [rt.LLM_NOTE in str(output) for output in outputs] == [False] * 5 + [True]
    assert outputs[5][-1]["type"] == "input_text", "the note as a part of the result's own kind (HTTP 400 otherwise)"


@pytest.mark.parametrize("count", [5, 7])
def test_fewer_failures_no_note_and_the_place_stays(count):
    document = codex_guessing(count)
    rt.llm_request(live.entry("lmstudio"), "responses", document)
    outputs = [str(item["output"]) for item in document["input"] if item.get("type") == "function_call_output"]
    assert [rt.LLM_NOTE in output for output in outputs] == [i == 5 for i in range(count)]
