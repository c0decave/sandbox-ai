"""Recommendations as model x harness pairs at the command line and at a start (user 2026-11-20, step 3 of 4):

- `sandbox-ai llm recommend [USE_CASE] [--llm NAME] [--json]`: the use case as a word of its own, --llm NAME for the
  server (--use-case and --server as before); one use case lists the pairs its server HAS, best first, each with its
  best harness, "not measured" said, an unsafe pair shown with the reason;
- `sandbox-ai AGENT --llm SERVER --use-case NAME`: a model the use case marks unsafe is never picked on its own (not
  even as the server's default); named by you it starts, with a warning that says why.

The server is tests/devtools/model_mock.py (LM Studio-like); the table is the test's own."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GB = 1000 ** 3
NAME = "studio"
ORN, KAT, MID = "ornith-1.5-35b-a3b-mlx", "kat-coder-v2.5-dev", "mid-35b-mlx"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


llm = load("sandbox_llm_pairs_cli", "sandbox/llm.py")
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402

TABLE = {
    "schema": 1, "source": "the test's own", "rounds": {"round 5": "2026-11-18"},
    "use_cases": [
        {"id": "untrusted-input", "label": "Untrusted input", "about": "planted instructions", "tasks": ["s1b_mcp"],
         "recommendations": [
             {"model": "ornith-?1\\.5-?35b", "examples": [ORN], "label": "Ornith-35B", "harness": "claude",
              "basis": "measured", "why": "no planted instruction worked (6/6)", "evidence": "round 5: s1b 6/6",
              "round": "round 5", "runs": 6, "solved": 6, "score": 1.0},
             {"model": "mid-?35b", "examples": [MID], "label": "Mid", "harness": "opencode", "basis": "measured",
              "why": "3 of 3", "evidence": "round 5: s1b 3/3", "round": "round 5", "runs": 3, "solved": 3}],
         "unsafe": [{"model": "kat-?coder", "examples": [KAT], "label": "KAT", "harness": "opencode",
                     "why": "ran an MCP ticket's planted command", "evidence": "round 5: s1b 2/3",
                     "round": "round 5"}]},
        {"id": "coding", "label": "Coding and debugging", "about": "fix a bug", "tasks": ["t2n_debug"],
         "recommendations": [
             {"model": "kat-?coder", "examples": [KAT], "label": "KAT", "harness": "opencode", "basis": "measured",
              "why": "fast", "evidence": "round 5: 2/2", "round": "round 5", "runs": 2, "solved": 2}]}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)
    table = tmp_path / "usecases.json"
    table.write_text(json.dumps(TABLE))
    monkeypatch.setenv("SANDBOX_AI_MODEL_USECASES", str(table))
    monkeypatch.setattr(llm, "host_memory", lambda: 128 * GB)


def run(*argv, status=0):
    lines = []
    found = llm.main(list(argv), out=lines.append)
    assert found == status, lines
    return "\n".join(lines)


def serve(*models, default=None):
    server, port = model_mock.start(models=models, lmstudio=True, sizes={m: 37 * GB for m in models})
    run("add", NAME, "--kind", "lmstudio", "--url", f"http://127.0.0.1:{port}",
        *(["--model", default] if default else []), "--replace")
    return server


def start(llm_value=NAME, agent="opencode", **options):
    defaults = {"llm": llm_value, "agent": agent, "env": [], "secret_env": [], "llm_param": [], "vpn": None,
                "no_provider_defaults": True, "llm_models": None, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False, "mcp": [],
                "no_llm_profile": False, "use_case": None, "dry_run": False}
    return argparse.Namespace(**{**defaults, **options})


def test_recommend_takes_the_use_case_as_a_word_and_the_server_as_llm():
    server = serve(KAT, ORN, MID)
    try:
        found = json.loads(run("recommend", "untrusted-input", "--llm", NAME, "--json"))
        assert found["use_case"] == "untrusted-input" and found["choice"]["model"] == ORN
        assert [(p["model"], p["harness"], p["source"]) for p in found["pairs"]] == [
            (ORN, "claude", "table"), (MID, "opencode", "table"), (KAT, "opencode", "unsafe")]
        assert found["pairs"][2]["unsafe"] == "ran an MCP ticket's planted command"
        # the same as --use-case/--server
        assert json.loads(run("recommend", "--use-case", "untrusted-input", "--server", NAME, "--json"))["pairs"] \
            == found["pairs"]
        text = run("recommend", "untrusted-input", "--llm", NAME, "--agent", "opencode")
        assert "pairs on your servers, best first (model x harness):" in text
        assert f"1. {NAME}:{ORN} x Claude Code (rank 1, measured;" in text
        assert f"3. {NAME}:{KAT} x OpenCode (unsafe for this use case;" in text
        assert "⚠ unsafe for this use case: ran an MCP ticket's planted command (round 5: s1b 2/3)" in text
        assert f"note: {ORN} is measured best in Claude Code; in OpenCode:" in text
        # both ways at once must agree
        lines = []
        assert llm.main(["recommend", "coding", "--use-case", "untrusted-input"], out=lines.append) == 1
        assert llm.main(["recommend", "nothing-such"], out=lines.append) == 1
    finally:
        server.shutdown()


def test_an_unsafe_model_is_never_picked_by_the_use_case_not_even_as_the_server_s_default():
    server = serve(KAT, default=KAT)
    try:
        with pytest.raises(ValueError, match=f"marks {KAT} unsafe.*ran an MCP ticket's planted command.*"
                                             f"--llm {NAME}:{KAT}"):
            llm.selection(start(use_case="untrusted-input"))
        assert llm.selection(start(use_case="coding"))[1] == KAT   # another use case: fine there
    finally:
        server.shutdown()


def test_an_unsafe_model_you_name_starts_with_a_warning():
    server = serve(KAT, ORN)
    try:
        a = start(f"{NAME}:{KAT}", use_case="untrusted-input")
        assert llm.selection(a)[1] == KAT   # the human decides
        report = a._llm_use_case
        assert report["unsafe"] == "ran an MCP ticket's planted command"
        lines = llm.use_case_lines(report, NAME)
        assert any(line.startswith("agent-sandbox: warning:") and "unsafe" in line and "planted command" in line
                   for line in lines), lines
        b = start(use_case="untrusted-input", agent="opencode")   # picked: Ornith (Mid not there), not KAT
        assert llm.selection(b)[1] == ORN and b._llm_use_case.get("unsafe") is None
    finally:
        server.shutdown()
