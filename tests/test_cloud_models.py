"""The table of cloud models (sandbox/cloud_models.json, cloud_models.py): its schema, an entry for every agent, effort
levels each agent's own flag takes, the launcher's --model/--effort checks agreeing with it, `sandbox-ai models`
(also through the real ./sandbox-ai), and the file shipped with the package."""
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cm = load("sandbox_cloud_models_test", "cloud_models.py")
s = load("sandbox_cloud_models_launcher", "agent_sandbox.py")
TABLE = cm.load()
AGENTS = list(s.AGENT_EFFORTS)


def test_the_table_has_no_problems_and_its_schema_is_checked():
    assert cm.problems(TABLE) == []
    assert TABLE["version"] == cm.VERSION and re.fullmatch(r"\d{4}-\d{2}-\d{2}", TABLE["checked"])
    broken = copy.deepcopy(TABLE)
    claude = broken["agents"]["claude"]["models"]
    claude[0]["efforts"] = ["low", "ultra"]          # a level Claude Code's --effort does not take
    claude[1]["id"] = claude[2]["id"]                # listed twice
    claude[3]["id"] = "opus --dangerously-skip-permissions"
    claude[4]["default_effort"] = "minimal"          # not one of its levels
    claude[5]["source"] = "http://example.org"       # not https
    claude[6]["context"] = -1
    claude[7]["checked"] = "yesterday"
    broken["agents"]["codex"]["surprise"] = True
    found = "\n".join(cm.problems(broken))
    for words in ("models[0].efforts", "is listed twice", "models[3].id", "models[4].default_effort",
                  "models[5].source", "models[6].context", "models[7].checked", "agents.codex.surprise"):
        assert words in found, words
    assert cm.problems({"version": 2}) == ["not a table of cloud models of version 1"]


def test_a_damaged_table_is_refused_whole(tmp_path):
    path = tmp_path / "cloud_models.json"
    path.write_text("{not json")
    with pytest.raises(ValueError, match="cannot be read"):
        cm.load(path)
    damaged = copy.deepcopy(TABLE)
    damaged["agents"]["claude"]["efforts"] = "low"
    path2 = tmp_path / "two.json"
    path2.write_text(json.dumps(damaged))
    with pytest.raises(ValueError, match="agents.claude.efforts"):
        cm.load(path2)


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_has_an_entry_and_its_levels_are_its_flags(agent):
    entry = TABLE["agents"][agent]
    assert entry["name"]
    # the agent's levels are those the launcher passes on (AGENT_EFFORTS: --effort, -c model_reasoning_effort=,
    # --reasoning, --thinking, the settings each agent reads)
    assert tuple(entry["efforts"]) == s.AGENT_EFFORTS[agent]
    if not entry["efforts"]:
        assert entry["effort_note"], "where the agent sets its effort instead"
    assert entry["models"] or entry["note"], "an empty list says why"
    for model in entry["models"]:
        assert re.fullmatch(s.MODEL_NAME, model["id"]), model["id"]
        assert model["source"].startswith("https://") and model["checked"]
        if model["efforts"] is not None:
            assert set(model["efforts"]) <= set(s.AGENT_EFFORTS[agent]), model
            assert model["default_effort"] in (None, *model["efforts"])
        else:
            assert model["default_effort"] is None


def test_every_agent_of_the_launcher_and_only_those():
    assert set(TABLE["agents"]) == set(AGENTS) == set(s.AGENT_MODELS)


def test_the_launchers_known_models_are_the_tables():
    assert s.AGENT_MODELS == cm.known_ids()
    assert {"fable", "opus", "sonnet", "haiku", "claude-opus-5-5"} <= set(s.AGENT_MODELS["claude"])
    assert all(model.startswith(("gpt-", "o")) for model in s.AGENT_MODELS["codex"])
    assert "anthropic/claude-opus-5-5" in s.AGENT_MODELS["opencode"] and "deepseek-v4-pro" in s.AGENT_MODELS["dsh"]


def test_efforts_follow_the_model_else_the_agent():
    assert cm.efforts("claude", "claude-opus-4-6") == ("low", "medium", "high", "max")
    assert cm.efforts("claude", "claude-haiku-4-5") == ()
    assert cm.efforts("claude", "my-own-model") == s.AGENT_EFFORTS["claude"]
    assert cm.efforts("claude", "") == s.AGENT_EFFORTS["claude"]
    assert cm.efforts("qwen", "coder-model") == s.AGENT_EFFORTS["qwen"]  # not known per model: the agent's
    assert cm.efforts("codex", "gpt-5.5") == ("low", "medium", "high", "xhigh")
    assert cm.default_effort("claude", "opus") == "medium" and cm.default_effort("claude", "nope") is None


def test_effort_problem_and_unknown_note_texts():
    assert cm.effort_problem("claude", "claude-opus-4-6", "xhigh") == \
        "--effort for claude-opus-4-6 in Claude Code is one of low, medium, high, max: 'xhigh'"
    assert cm.effort_problem("claude", "claude-haiku-4-5", "low") == \
        "--effort: claude-haiku-4-5 has no effort levels in Claude Code; start without --effort"
    for fine in (("claude", "claude-opus-4-6", "max"), ("claude", "unknown-model", "xhigh"), ("claude", "", "low"),
                 ("qwen", "coder-model", "max"), ("claude", "opus", None)):
        assert cm.effort_problem(*fine) is None, fine
    assert cm.unknown_note("claude", "my-model") == ("my-model is not in the known list of Claude Code's models "
                                                     "(sandbox-ai models --agent claude): it is passed on as it is")
    assert cm.unknown_note("claude", "opus") is None and cm.unknown_note("claude", "") is None


def sample_effort(agent, model):
    """A level AGENT takes that MODEL does not (None: it takes them all)."""
    return next((level for level in s.AGENT_EFFORTS[agent] if level not in model["efforts"]), None)


@pytest.mark.parametrize("agent", [a for a in AGENTS if a not in ("opencode", "opencode2", "dsh", "letta")])
def test_the_launcher_agrees_with_the_table(agent, capsys):
    """Each listed model: its own levels pass, another level of the agent is refused with the table's words; a model
    not listed takes every level of the agent."""
    for model in TABLE["agents"][agent]["models"]:
        if model["efforts"] is None:
            for level in s.AGENT_EFFORTS[agent]:
                s.check_model_effort(s.parse_args([agent, "--model", model["id"], "--effort", level]))
            continue
        for level in model["efforts"]:
            s.check_model_effort(s.parse_args([agent, "--model", model["id"], "--effort", level]))
        other = sample_effort(agent, model)
        if other is not None:
            # (a warning, never a refusal: the table may lag behind the agent, which lowers or refuses a level itself)
            capsys.readouterr()
            s.check_model_effort(s.parse_args([agent, "--model", model["id"], "--effort", other]))
            assert cm.effort_problem(agent, model["id"], other) in capsys.readouterr().err
    for level in s.AGENT_EFFORTS[agent]:
        s.check_model_effort(s.parse_args([agent, "--model", "not-a-listed-model", "--effort", level]))


@pytest.mark.parametrize("agent", ["opencode", "opencode2", "dsh", "letta"])
def test_agents_without_an_effort_flag_still_refuse_it(agent):
    model = TABLE["agents"][agent]["models"][0]["id"]
    s.check_model_effort(s.parse_args([agent, "--model", model]))
    with pytest.raises(ValueError, match="sets its effort per model in its own configuration"):
        s.check_model_effort(s.parse_args([agent, "--model", model, "--effort", "high"]))


def test_codex_takes_the_levels_of_its_current_models(capsys):
    for level in ("none", "max", "ultra"):
        s.check_model_effort(s.parse_args(["codex", "--effort", level]))
    assert s.model_effort_args(s.parse_args(["codex", "--model", "gpt-6-sol", "--effort", "ultra"])) == [
        "--model", "gpt-6-sol", "-c", "model_reasoning_effort=ultra"]
    capsys.readouterr()
    s.check_model_effort(s.parse_args(["codex", "--model", "gpt-6-luna", "--effort", "ultra"]))  # (warned, passed on)
    assert "--effort for gpt-6-luna in Codex is one of low, medium, high, xhigh, max" in capsys.readouterr().err


def test_models_command_lists_and_prints_json(capsys):
    assert cm.main(["--agent", "claude", "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert list(document["agents"]) == ["claude"] and document["version"] == 1
    assert document["agents"]["claude"] == TABLE["agents"]["claude"]
    assert cm.main(["--agent", "dsh"]) == 0
    text = capsys.readouterr().out
    assert "DeepSeek Harness (dsh) - effort: --effort is refused" in text and "deepseek-v4-pro" in text
    assert cm.main([]) == 0
    text = capsys.readouterr().out
    assert all(f"({agent})" in text for agent in AGENTS)
    assert "    claude-opus-4-6" in text and "effort: low, medium, high, max (default high)" in text
    assert cm.main(["--agent", "nobody"]) == 2
    assert "no agent 'nobody'" in capsys.readouterr().err


def test_models_through_the_real_command():
    """e2e: the dispatcher runs the command, as a user (and the web interface on a host) does."""
    done = subprocess.run([str(ROOT / "sandbox-ai"), "models", "--agent", "codex", "--json"], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout)["agents"]["codex"]["effort_option"] == "-c model_reasoning_effort=LEVEL"
    done = subprocess.run([str(ROOT / "sandbox-ai"), "models", "--help"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0 and "--agent AGENT" in done.stdout


def test_the_table_is_shipped():
    assert "cloud_models.json" in (ROOT / "MANIFEST.in").read_text()
    pyproject = (ROOT / "pyproject.toml").read_text()
    assert '"cloud_models.json"' in pyproject
