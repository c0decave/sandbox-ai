"""A start with a model server of your own (--llm) whose context window for the model is not known (the server does
not say it, it sits behind a VPN, SANDBOX_AI_LLM_NO_CONTEXT_FETCH=1): only Claude Code got a note
(llm.claude_window_environment); every other agent silently used its own default window, which can be far off - it
compacts too late (the server refuses or cuts the conversation) or too early. Now every agent's start says what that
agent assumes (its own sources, the versions sandbox-ai builds: Codex' fallback 272,000 tokens, OpenCode's window 0 -
no compaction of its own -, Qwen Code's 200,000, Goose's 128,000, nanobot's 200,000, Pi's 128,000, Hermes' probe else
256,000, OpenHands' LiteLLM catalog, Letta Code's 128,000) and how to set the window (llm set ... --param num_ctx,
--llm-param num_ctx) - once per start; nothing is written for the window then (no guess of sandbox-ai's: a made-up
window is wrong one way or the other). Letta Code has no setting sandbox-ai can give: it is also warned about when
the window is known and smaller than the 128,000 it assumes. A public service (OpenRouter and the like), whose
models the agents' catalogs know, gets no note."""
import argparse
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_llm_unknown_window", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
NAME, MODEL = "box", "qwen3-coder-next"
SET = f"sandbox-ai llm set {NAME} --model {MODEL} --param num_ctx=TOKENS"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.setenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", "1")  # the host does not ask: the window is not known
    monkeypatch.setattr(llm, "running_runs", lambda: {})
    lines = []
    assert llm.main(["add", NAME, "--kind", "vllm", "--url", "http://127.0.0.1:9", "--model", MODEL],
                    out=lines.append) == 0, lines


def start(agent, *params, **options):
    defaults = {"llm": f"{NAME}:{MODEL}", "agent": agent, "env": [], "secret_env": [], "llm_param": list(params),
                "vpn": None, "no_provider_defaults": True, "llm_models": None, "offline": False, "no_inspect": [],
                "agent_args": [], "local_config": False, "dangerously_skip_permissions": False}
    return argparse.Namespace(**{**defaults, **options})


def written_home(a):
    written = {}
    llm.prepare_home(a, Path("/h"), lambda data, home, relative: written.update({relative: data.decode()}), None, None)
    return written


def told(agent, a):
    """The window AGENT is told for the start's model (None: not told), from what sandbox-ai writes for it."""
    if agent == "codex":
        text = written_home(a)[f".codex/{llm.CODEX_PROFILE}.config.toml"]
        found = [line for line in text.splitlines() if line.startswith("model_context_window = ")]
        return int(found[0].split(" = ")[1]) if found else None
    if agent == "opencode":
        config = json.loads(written_home(a)[llm.OPENCODE_CONFIG])
        return next(iter(config["provider"].values()))["models"][MODEL].get("limit", {}).get("context")
    if agent == "opencode2":
        config = json.loads(llm.opencode2_files(a)["sandbox-ai-llm.json"])
        return next(iter(config["provider"].values()))["models"][MODEL].get("limit", {}).get("context")
    if agent == "qwen":
        return llm.qwen_settings(a)["modelProviders"]["openai"][0]["generationConfig"].get("contextWindowSize")
    if agent == "goose":
        value = llm.goose_environment(a, *llm.selection(a)).get("GOOSE_CONTEXT_LIMIT")
        return int(value) if value else None
    if agent == "dsh":
        return llm.dsh_config(a)["route"]["models"][0].get("contextWindow")
    if agent == "pi":
        return llm.pi_llm(a)["provider"]["config"]["models"][0].get("contextWindow")
    if agent == "openhands":
        return llm.openhands_llm(a).get("max_input_tokens")
    if agent == "nanobot":
        return llm.nanobot_config(a)["agents"]["defaults"].get("contextWindowTokens")
    if agent == "hermes":
        return llm.hermes_config(a)["model"].get("context_length")
    if agent == "letta":
        llm.agent_environment(a)
        return None  # Letta Code takes no window from sandbox-ai
    raise AssertionError(agent)


# What each agent assumes, as the note says it (llm.AGENT_WINDOWS).
ASSUMED = {"codex": "272,000", "opencode": "window of 0", "opencode2": "window of 0", "qwen": "200,000",
           "goose": "128,000", "dsh": "its own default", "pi": "128,000", "openhands": "LiteLLM",
           "nanobot": "200,000", "hermes": "256,000", "letta": "128,000"}


@pytest.mark.parametrize("agent", sorted(ASSUMED))
def test_an_unknown_window_says_what_the_agent_assumes_and_how_to_set_it(agent, capsys):
    a = start(agent)
    assert told(agent, a) is None  # no window of sandbox-ai's guessing
    err = capsys.readouterr().err
    label = llm.AGENT_WINDOWS[agent][0]
    assert f"--llm {NAME}: the context window of {MODEL} is not known" in err, err
    assert label in err and ASSUMED[agent] in err
    if agent == "letta":
        assert "no setting" in err and "Letta Code" in err
    else:
        assert SET in err and "--llm-param num_ctx=TOKENS" in err
    # Once per start, however often the configuration is made (the launcher asks more than once).
    told(agent, a)
    assert "is not known" not in capsys.readouterr().err


@pytest.mark.parametrize("agent", sorted(set(ASSUMED) - {"letta"}))
def test_a_window_you_set_is_told_and_no_note_comes(agent, capsys):
    assert told(agent, start(agent, "num_ctx=65536")) == 65536
    assert "is not known" not in capsys.readouterr().err


def test_letta_is_warned_about_a_known_window_below_what_it_assumes(capsys):
    """Letta Code takes no window from sandbox-ai and assumes 128,000 tokens (at most) for a server that is not
    llama.cpp: a smaller window it is never told is said. llama.cpp's own /props it reads itself: no note."""
    told("letta", start("letta", "num_ctx=32768"))
    err = capsys.readouterr().err
    assert "Letta Code" in err and "128,000" in err and "32768" in err and "is not known" not in err
    told("letta", start("letta", "num_ctx=131072"))
    assert "Letta Code" not in capsys.readouterr().err
    lines = []
    assert llm.main(["add", NAME, "--kind", "llamacpp", "--url", "http://127.0.0.1:9", "--model", MODEL,
                     "--replace"], out=lines.append) == 0, lines
    told("letta", start("letta", "num_ctx=32768"))
    told("letta", start("letta"))
    assert "Letta Code" not in capsys.readouterr().err


def test_a_public_service_gets_no_note(capsys, monkeypatch):
    """OpenRouter and the like: the agents' catalogs know their models; nothing is loaded for you."""
    found = {"name": "router", "kind": "openai-compatible", "parsed": {"loopback": False, "address": None,
                                                                       "host": "openrouter.ai"}}
    llm.unknown_window_note(start("goose"), found, MODEL, None, "goose")
    assert capsys.readouterr().err == ""


def test_claude_code_keeps_its_own_note(capsys):
    """Claude Code's note stays claude_window_environment's (it names what Claude Code says): not a second one."""
    env = llm.agent_environment(start("claude"))
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in env
    err = capsys.readouterr().err
    assert err.count(f"{MODEL}") and err.count("Claude Code does not know the model") == 1
    llm.unknown_window_note(start("claude"), llm.selection(start("claude"))[0], MODEL, None, "claude")
    assert capsys.readouterr().err == ""


def test_e2e_an_opencode_start_says_what_opencode_assumes(tmp_path):
    """The real ./sandbox-ai (only Podman stood in for, live_fakes.py): `opencode --llm box:MODEL` with a server
    whose window the host does not learn - the start says what OpenCode assumes and how to set it, once, and writes
    no window; with --llm-param num_ctx the window is written and nothing is said."""
    import sys
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "host")
    host.add_image("localhost/opencode-sandbox:latest")
    env = {"SANDBOX_AI_LLM_NO_CONTEXT_FETCH": "1", "SANDBOX_AI_SECRETS": "file"}
    added = host.run("llm", "add", NAME, "--kind", "vllm", "--url", "http://127.0.0.1:9", "--model", MODEL, env=env)
    assert added.returncode == 0, added.stderr
    for params, kept in (((), "k1"), (("--llm-param", "num_ctx=65536"), "k2")):
        done = host.run("opencode", "--no-tmux", "--llm", f"{NAME}:{MODEL}", *params,
                        env={**env, "FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / kept)})
        assert done.returncode == 0, done.stderr
        configs = sorted(host.directory.rglob("sandbox-ai-llm.json"), key=lambda p: p.stat().st_mtime)
        model = next(iter(json.loads(configs[-1].read_text())["provider"].values()))["models"][MODEL]
        if params:
            assert model["limit"]["context"] == 65536 and "is not known" not in done.stderr
        else:
            assert "limit" not in model
            assert done.stderr.count(f"the context window of {MODEL} is not known") == 1, done.stderr
            assert "OpenCode takes a window of 0" in done.stderr and SET in done.stderr
