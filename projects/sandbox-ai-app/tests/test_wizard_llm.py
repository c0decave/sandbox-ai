"""An own model server in the wizard (sandbox_ai_app/wizard_llm.py, no Qt): the arguments of `sandbox-ai llm add` and
`llm check`, the listings read back, which agent can use which server (OpenCode and Codex every one), and what goes
on the board for every agent x server x model x "only this server" - --llm NAME[:MODEL], --no-provider-defaults, and
never --model with it (the launcher refuses that pair: it kept OpenCode from starting with a local model)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest

from sandbox_ai_app import backend, board as B, presets as PR, wizard_llm as WL

HAS_SAI = bool(os.environ.get("SANDBOX_AI_HOME") or backend.shutil.which("sandbox-ai"))
needs_sai = pytest.mark.skipif(not HAS_SAI, reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")


class FakeSai:
    """call() answers from a table; every call is kept."""

    def __init__(self, answers=None, llm=None):
        self.answers, self.calls, self.llm = answers or {}, [], llm

    def call(self, *args, timeout=120, input=None):
        self.calls.append(args)
        for prefix, answer in self.answers.items():
            if args[:len(prefix)] == prefix:
                return answer
        return 0, "", ""

    def module(self, name):
        if self.llm is None:
            raise FileNotFoundError(name)
        return self.llm


def real_llm():
    home = os.environ.get("SANDBOX_AI_HOME")
    path = Path(home) / "sandbox" / "llm.py" if home else None
    if path is None or not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("sandbox_llm_for_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- llm add, llm check, llm models --

@pytest.mark.parametrize("kind,port", [("lmstudio", 1234), ("ollama", 11434), ("llamacpp", 8080), ("vllm", 8000)])
def test_each_kind_gets_this_machine_with_its_usual_port(kind, port):
    assert WL.default_port(kind) == port
    assert WL.default_url(kind) == f"http://127.0.0.1:{port}"
    assert WL.add_argv({"name": kind, "kind": kind, "url": WL.default_url(kind)}) == \
        ["llm", "add", kind, "--kind", kind, "--url", f"http://127.0.0.1:{port}"]


def test_an_openai_compatible_api_has_no_usual_port_and_may_offer_the_anthropic_api():
    assert WL.default_port("openai-compatible") is None and WL.default_url("openai-compatible") == ""
    fields = {"name": "gateway", "kind": "openai-compatible", "url": "https://llm.example.org/v1",
              "secret": "LLM_KEY", "vpn": "home", "anthropic": True}
    assert WL.add_argv(fields) == ["llm", "add", "gateway", "--kind", "openai-compatible", "--url",
                                   "https://llm.example.org/v1", "--api-key-secret", "LLM_KEY", "--vpn", "home",
                                   "--anthropic"]
    # --anthropic only where it means something (the other kinds offer the Messages API themselves).
    assert "--anthropic" not in WL.add_argv({**fields, "kind": "vllm"})


def test_the_ports_are_sandbox_ais_own():
    llm = real_llm()
    if llm is None:
        pytest.skip("no sandbox-ai checkout")
    for kind, _, port in WL.KINDS:
        assert llm.KINDS[kind]["port"] == port, kind
        assert WL.default_port(kind, FakeSai(llm=llm)) == port
    assert [k for k, _, _ in WL.KINDS] and set(llm.KINDS) == {k for k, _, _ in WL.KINDS}


@pytest.mark.parametrize("fields", [
    {"name": "-x", "kind": "ollama", "url": "http://127.0.0.1:11434"},
    {"name": "a:b", "kind": "ollama", "url": "http://127.0.0.1:11434"},
    {"name": "ok", "kind": "nope", "url": "http://127.0.0.1:11434"},
    {"name": "ok", "kind": "ollama", "url": "ftp://127.0.0.1"},
    {"name": "ok", "kind": "ollama", "url": "http://user@127.0.0.1:11434"},
    {"name": "ok", "kind": "ollama", "url": "http://127.0.0.1:11434?x=1"},
    {"name": "ok", "kind": "ollama", "url": "http://127.0.0.1:11434", "secret": "1KEY"},
    {"name": "ok", "kind": "ollama", "url": "http://127.0.0.1:11434", "secret": "--x"},
    {"name": "ok", "kind": "ollama", "url": "http://127.0.0.1:11434", "vpn": "--all"},
])
def test_what_cannot_go_to_sandbox_ai_is_refused(fields):
    with pytest.raises(ValueError):
        WL.add_argv(fields)


def test_check_and_models_arguments():
    assert WL.check_argv("lm") == ["llm", "check", "lm"]
    for agent in WL.AGENTS:
        assert WL.check_argv("lm", agent) == ["llm", "check", "lm", "--agent", agent]
    assert WL.check_argv("lm", "opencode", "qwen2.5-coder:7b") == \
        ["llm", "check", "lm", "--agent", "opencode", "--model", "qwen2.5-coder:7b"]
    assert WL.check_argv("lm", "other") == ["llm", "check", "lm"]
    assert WL.models_argv("lm") == ["llm", "models", "lm", "--json"]
    for bad in ("-lm", "", "a b"):
        with pytest.raises(ValueError):
            WL.check_argv(bad)
        with pytest.raises(ValueError):
            WL.models_argv(bad)
    with pytest.raises(ValueError):
        WL.check_argv("lm", "codex", "--model")


def test_add_then_the_key_then_the_check_for_the_agent():
    sai, order = FakeSai(), []
    result = WL.add_and_check(sai, {"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234",
                                    "secret": "LLM_KEY"}, "opencode",
                              keep_key=lambda: order.append(len(sai.calls)) or (0, "", ""))
    assert sai.calls == [("llm", "add", "lm", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234",
                          "--api-key-secret", "LLM_KEY"), ("llm", "check", "lm", "--agent", "opencode")]
    assert order == [1], "the key is kept after the add, before the check"
    assert result["name"] == "lm" and result["added"][0] == 0 and result["kept"] == (0, "", "") \
        and result["checked"][0] == 0


def test_a_refused_server_keeps_no_key_and_is_not_checked():
    sai, kept = FakeSai({("llm", "add"): (1, "", "sandbox-ai llm: an endpoint lm exists")}), []
    result = WL.add_and_check(sai, {"name": "lm", "kind": "ollama", "url": "http://127.0.0.1:11434"}, "codex",
                              keep_key=lambda: kept.append(1))
    assert len(sai.calls) == 1 and not kept and result["checked"] is None and result["kept"] is None
    assert "exists" in WL.message(*result["added"])
    assert WL.message(3, "", "") == "exit status 3"


def test_the_listings_are_read_and_checked():
    entries = [{"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "agents": ["claude", "codex",
                                                                                            "opencode"]},
               {"name": "-bad", "kind": "vllm"}, "junk"]
    out = "broken: unusable (no url)\n" + json.dumps(entries)
    found, notes = WL.list_endpoints(FakeSai({("llm", "list"): (0, out, "")}))
    assert [e["name"] for e in found] == ["lm"] and notes == ["broken: unusable (no url)"]
    assert WL.parse_endpoints("[]") == ([], [])
    with pytest.raises(ValueError):
        WL.list_endpoints(FakeSai({("llm", "list"): (1, "", "sandbox-ai llm: llm.json is not private")}))
    with pytest.raises(ValueError):
        WL.parse_endpoints("{}")
    models = [{"name": "qwen2.5-coder:7b", "size": 4_700_000_000, "loaded": True, "details": "Q4\x1b[31m"},
              {"name": "--evil"}, {"name": "x", "size": -1, "loaded": "yes"}, 7]
    listed = WL.list_models(FakeSai({("llm", "models"): (0, json.dumps(models), "")}), "lm")
    assert [m["name"] for m in listed] == ["qwen2.5-coder:7b", "x"]
    assert listed[0]["size"] == 4_700_000_000 and listed[0]["loaded"] is True and "\x1b" not in listed[0]["details"]
    assert listed[1]["size"] is None and listed[1]["loaded"] is None
    with pytest.raises(ValueError, match="connection refused"):
        WL.list_models(FakeSai({("llm", "models"): (1, "", "sandbox-ai llm: connection refused")}), "lm")


# -- which agent can use which server --

LOCAL = {"name": "lm", "kind": "lmstudio", "url": "http://127.0.0.1:1234", "models": ["qwen2.5-coder-7b"]}
GATEWAY = {"name": "gw", "kind": "openai-compatible", "url": "https://llm.example.org", "models": []}
GATEWAY_ANTHROPIC = {**GATEWAY, "name": "gwa", "anthropic": True}


@pytest.mark.parametrize("endpoint", [LOCAL, GATEWAY, GATEWAY_ANTHROPIC])
def test_codex_and_opencode_can_use_every_server(endpoint):
    for agent in ("codex", "opencode"):
        assert WL.usable_by(endpoint, agent) and WL.agent_hint(endpoint, agent) == ""


def test_claude_needs_the_anthropic_messages_api():
    assert WL.usable_by(LOCAL, "claude") and WL.usable_by(GATEWAY_ANTHROPIC, "claude")
    assert not WL.usable_by(GATEWAY, "claude")
    assert "Anthropic Messages API" in WL.agent_hint(GATEWAY, "claude") and "OpenCode" in WL.agent_hint(GATEWAY, "claude")
    # What sandbox-ai says in "agents" decides.
    assert not WL.usable_by({**LOCAL, "agents": ["codex", "opencode"]}, "claude")
    assert WL.usable_by({**LOCAL, "agents": ["codex", "opencode"]}, "opencode")
    llm = real_llm()
    if llm is not None:  # sandbox-ai's own words, when its module loads
        assert "Codex or OpenCode" in WL.agent_hint(GATEWAY, "claude", FakeSai(llm=llm))


def test_rows_names_loopback_and_placeholder_choices():
    assert WL.endpoint_row({**LOCAL, "agents": ["claude", "codex", "opencode"]}) == \
        ["lm", "LM Studio", "http://127.0.0.1:1234", "claude, codex, opencode"]
    assert WL.loopback(LOCAL) and WL.loopback({"url": "http://localhost:11434"}) and \
        WL.loopback({"url": "http://[::1]:8000"}) and not WL.loopback(GATEWAY)
    assert WL.free_name("lmstudio", ["lmstudio", "lmstudio-2"]) == "lmstudio-3" and WL.free_name("ollama", []) == "ollama"
    assert WL.placeholder_choices(LOCAL) == ["lm", "lm:qwen2.5-coder-7b"]
    assert WL.placeholder_choices({"name": "-x"}) == []


# -- what goes on the board --

def test_the_model_for_llm():
    assert WL.chosen_model(LOCAL, "qwen2.5-coder:32b") == "qwen2.5-coder:32b"
    assert WL.chosen_model({**LOCAL, "model": "default-one"}, "") == ""  # the server's default does
    assert WL.chosen_model(LOCAL, "") == "qwen2.5-coder-7b"  # the only one it listed
    assert WL.chosen_model(GATEWAY, "", [{"name": "only"}]) == "only"
    with pytest.raises(ValueError, match="Choose or type a model"):
        WL.chosen_model(GATEWAY, "")
    with pytest.raises(ValueError):
        WL.chosen_model(LOCAL, "--model")


@pytest.mark.parametrize("name,model,only", [("lm", "qwen2.5-coder:7b", True), ("lm", "", False),
                                              ("gw", "gpt-oss-20b", False), ("gwa", "glm-4.6", True)])
def test_the_options_of_a_choice(name, model, only):
    options = WL.options({"name": name, "model": model, "only": only})
    assert options[:2] == ["--llm", f"{name}:{model}" if model else name]
    assert ("--no-provider-defaults" in options) is only and "--model" not in options
    assert WL.options(None) == []
    assert WL.parse_value(options[1]) == (name, model)
    assert WL.parse_value("qwen:7b:x") == ("qwen", "7b:x") and WL.parse_value("-x:y") == ("", "")


@needs_sai
@pytest.mark.parametrize("agent", ["claude", "codex", "opencode"])
@pytest.mark.parametrize("only", [True, False])
@pytest.mark.parametrize("model", ["qwen2.5-coder:7b", ""])
def test_any_agent_with_an_own_server_on_the_board(agent, only, model):
    """The user's request: OpenCode (or any agent) with a local model. A cloud model chosen before is dropped (the
    launcher refuses --model with --llm); "only this server" is --no-provider-defaults, unchecked it is gone."""
    sai = backend.SandboxAI()
    board = B.from_argv(sai, agent, ["--model", "some-cloud-model", "--no-provider-defaults"], workspaces=["/tmp/p"])
    WL.apply(board, {"name": "lm", "model": model, "only": only})
    argv = B.to_argv(sai, board)
    assert argv[argv.index("--llm") + 1] == ("lm:" + model if model else "lm")
    assert ("--no-provider-defaults" in argv) is only and "--model" not in argv
    assert B.values(board, sai)[0] == agent and WL.uses_server(board)
    assert WL.from_board(board) == {"name": "lm", "model": model, "only": only}
    WL.clear(board)
    argv = B.to_argv(sai, board)
    assert "--llm" not in argv and "--no-provider-defaults" not in argv and not WL.uses_server(board)
    assert WL.from_board(board) is None


@needs_sai
def test_the_llm_presets_take_the_wizards_server():
    """local-llm, mixed-llm (OpenCode) and remote-llm: the <llm:...> placeholder is the server chosen (with its model)
    and a VPN that follows it is the server's own; nothing is left to ask for then."""
    sai = backend.SandboxAI()
    choice = {"name": "gpu", "model": "qwen3-coder", "only": True}
    for ident in ("local-llm", "mixed-llm"):
        preset = PR.find(ident)
        values = WL.prefilled(preset, choice)
        assert values == {"<llm:local>": "gpu:qwen3-coder"} and not WL.needs_form(preset, values)
        board = PR.build(sai, preset, "/tmp/p", agent="opencode", values=values)
        argv = B.to_argv(sai, board)
        assert argv[argv.index("--llm") + 1] == "gpu:qwen3-coder" and B.values(board, sai)[0] == "opencode"
    remote = PR.find("remote-llm")
    assert WL.prefilled(remote, choice, {"vpn": "ts"}) == {"<llm:remote>": "gpu:qwen3-coder", "<vpn:tailnet>": "ts"}
    assert WL.needs_form(remote, WL.prefilled(remote, choice))
    assert WL.prefilled(remote, None) == {}


@needs_sai
@pytest.mark.parametrize("agent", ["opencode", "codex", "claude"])
def test_the_launcher_accepts_any_agent_with_a_registered_local_server(agent, tmp_path, monkeypatch):
    """End to end in a throwaway HOME: `sandbox-ai llm add` with the wizard's arguments, then a dry run of the board
    the wizard builds for AGENT - and the pair that blocked OpenCode before (--model with --llm) refused."""
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    sai = backend.SandboxAI()
    status, out, err = sai.call(*WL.add_argv({"name": "lm", "kind": "lmstudio", "url": WL.default_url("lmstudio")}),
                                timeout=60)
    assert status == 0, err
    found, _ = WL.list_endpoints(sai)
    assert [e["name"] for e in found] == ["lm"] and WL.usable_by(found[0], agent)
    (tmp_path / "ws").mkdir()
    board = B.with_backend(B.from_argv(sai, agent, [], workspaces=[str(tmp_path / "ws")]), "podman")
    WL.apply(board, {"name": "lm", "model": "qwen2.5-coder-7b", "only": True})
    plan, message = sai.dry_run(agent, B.launcher_argv(sai, board))
    assert plan is not None, message
    board.first("agent").props["model"] = "some-cloud-model"
    plan, message = sai.dry_run(agent, B.launcher_argv(sai, board))
    assert plan is None and "--model and --llm" in message
