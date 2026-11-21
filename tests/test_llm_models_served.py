"""`sandbox-ai llm models NAME` (and the app's model table, which reads its --json) never said what Ollama really
serves a model with: Ollama's listing (/api/tags) has no window, the window a start was told came from /api/show's
model_info - the model's own maximum (131072 here) - and the start alone learnt what Ollama serves
(llm.start_context, served_context). Now the listing of an Ollama server carries, per model, the window Ollama serves
its OpenAI-compatible requests with and how that was learnt, loading nothing: "loaded" (/api/ps), "modelfile" (its
Modelfile's num_ctx, /api/show), else "default" without a number (OLLAMA_CONTEXT_LENGTH, which Ollama's API does not
report; the start learns it by loading the model). Never remembered as the model's window (it changes with every
load); another kind's listing is as before. Against the Ollama-like mock server (tests/devtools/model_mock.py) and
the real ./sandbox-ai (end to end)."""
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
import model_mock  # noqa: E402

SPEC = importlib.util.spec_from_file_location("sandbox_llm_models_served", ROOT / "sandbox/llm.py")
llm = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(llm)
LOADED, OWN, PLAIN = "qwen2.5-coder:7b", "llama3.1:8b", "gemma3:4b"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    monkeypatch.delenv("SANDBOX_AI_LLM_NO_CONTEXT_FETCH", raising=False)


@pytest.fixture
def ollama(tmp_path):
    """An Ollama-like server: LOADED loaded with 16384, OWN with a Modelfile num_ctx 8192, PLAIN neither."""
    log = tmp_path / "requests.jsonl"
    server, port = model_mock.start(models=(LOADED, OWN, PLAIN), ollama_ps=True, log=str(log), log_bodies=True,
                                    running={LOADED: {"context": 16384, "expires_at": "2318-01-01T00:00:00Z"}},
                                    modelfile_context={OWN: 8192})
    lines = []
    assert llm.main(["add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", "--model", LOADED],
                    out=lines.append) == 0, lines
    yield port, log
    server.shutdown()


def listed(*argv):
    lines = []
    assert llm.main(["models", "ol", *argv], out=lines.append) == 0, lines
    return lines


def test_the_listing_says_the_window_ollama_serves_and_how_it_was_learnt(ollama):
    _, log = ollama
    models = {m["name"]: m for m in json.loads("\n".join(listed("--json")))}
    assert (models[LOADED]["served_context"], models[LOADED]["served_how"]) == (16384, "loaded")
    assert (models[OWN]["served_context"], models[OWN]["served_how"]) == (8192, "modelfile")
    assert "served_context" not in models[PLAIN] and models[PLAIN]["served_how"] == "default"
    # Not the model's own maximum (131072, /api/show's model_info), not a "context" remembered for the start.
    assert all(m.get("context") is None for m in models.values())
    assert not llm.endpoint("ol").get("contexts")
    # Nothing loaded to learn it.
    paths = [json.loads(line)["path"] for line in log.read_text().splitlines()]
    assert "/api/generate" not in paths and "/api/chat" not in paths


def test_the_table_shows_it(ollama):
    table = "\n".join(listed())
    assert "CONTEXT" in table and "16k" in table and "8k" in table and "default" in table
    assert "128k" not in table


def test_a_server_that_cannot_say_leaves_the_listing_as_it_was(ollama, monkeypatch):
    """/api/show refused (an old Ollama), /api/ps unreadable: the listing still comes, without what is not known."""
    real = llm.Client.json

    def refusing(self, path, *args, **kwargs):
        if path in ("/api/show", "/api/ps"):
            raise ValueError("refused")
        return real(self, path, *args, **kwargs)
    monkeypatch.setattr(llm.Client, "json", refusing)
    models = json.loads("\n".join(listed("--json")))
    assert [m["name"] for m in models] and all("served_context" not in m and "served_how" not in m for m in models)


def test_the_budget_ends_the_asking(ollama, monkeypatch):
    """A server with many models: what is not asked within the budget is left out (the listing never waits long)."""
    port, _ = ollama
    clock = iter(range(0, 1000, 20))
    monkeypatch.setattr(llm.time, "monotonic", lambda: next(clock))
    client = llm.Client(llm.endpoint("ol"))
    models = [{"name": n} for n in (LOADED, OWN, PLAIN)]
    llm.served_windows(llm.endpoint("ol"), models, client, budget=30)
    assert models[0]["served_how"] == "loaded" and "served_how" not in models[2]


def test_another_kind_is_left_as_it_is():
    models = [{"name": "m", "context": 4096}]
    llm.served_windows({"kind": "lmstudio", "name": "s"}, models, client=None)
    assert models == [{"name": "m", "context": 4096}]


def test_e2e_llm_models_of_ollama_through_the_real_command(ollama, tmp_path):
    from live_fakes import Host
    port, _ = ollama
    host = Host(tmp_path / "host")
    env = {"SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_LLM_NO_CONTEXT_FETCH": ""}
    assert host.run("llm", "add", "ol", "--kind", "ollama", "--url", f"http://127.0.0.1:{port}", env=env).returncode == 0
    done = host.run("llm", "models", "ol", "--json", env=env)
    assert done.returncode == 0, done.stderr
    models = {m["name"]: m for m in json.loads(done.stdout)}
    assert models[OWN]["served_context"] == 8192 and models[PLAIN]["served_how"] == "default"
