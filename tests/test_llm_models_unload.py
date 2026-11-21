"""A start limited to some models of its --llm server (--llm-models, --llm NAME:MODEL,...) lets no request name
another model (runtime.llm_model_refusal). LM Studio's POST /api/v1/models/unload names no model, though: it takes an
instance id ({"instance_id": ...}), so an agent of a limited start could unload any model of the server - another
sandbox's among them. The relay now reads the instance id the way LM Studio makes it: the model's key for its first
instance, KEY:N for a further one (llm_instance_model); an instance of a model the start offers may go, any other is
refused, and so is an identifier of its own (lms load --identifier on the server's machine), which names no model
the relay can know without asking the server - fail closed. Through the real connection proxy against the LM
Studio-like mock server (tests/devtools/model_mock.py)."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests/devtools"))
sys.path.insert(0, str(ROOT / "tests"))
from test_hotfix_llm_models import MODEL, NAME, OTHERS, isolated, llm, requests_of, rt, start, studio  # noqa: E402,F401
import model_mock  # noqa: E402


@pytest.mark.parametrize("instance,model", [
    ("a", "a"), ("a:2", "a"), ("a:17", "a"), ("org/b-7b", "org/b-7b"), ("org/b-7b:3", "org/b-7b"),
    ("c:8b", "c:8b"), ("c:8b:2", "c:8b"),        # (a key with a colon of its own)
    ("z", None), ("z:2", None), ("a:", None), ("a:x", None), ("a:0", None), ("a:1", None), ("a:02", None),
    ("a:\u0662", None),                          # (a unicode digit)
    ("a:99999", None), (":2", None), ("", None), (5, None), (None, None), (["a"], None), ({"a": 1}, None),
])
def test_an_instance_belongs_to_the_model_lm_studio_names_it_by(instance, model):
    assert rt.llm_instance_model(instance, ["a", "org/b-7b", "c:8b"]) == model


def test_an_unload_of_another_models_instance_is_refused():
    entry = {"name": "s", "models": ["a", "b"]}
    assert rt.llm_model_refusal(entry, {"instance_id": "a"}) is None
    assert rt.llm_model_refusal(entry, {"instance_id": "b:2"}) is None
    why = rt.llm_model_refusal(entry, {"instance_id": "other-model:2"})
    assert "the instance other-model:2" in why and "a, b" in why and "--llm-models all" in why
    assert "sandbox-ai llm unload s" in why
    assert "given as int" in rt.llm_model_refusal(entry, {"instance_id": 7})
    # Any model of the server: nothing to refuse.
    assert rt.llm_model_refusal({"name": "s", "models": None}, {"instance_id": "x"}) is None


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_the_relay_lets_a_limited_start_unload_only_its_own_models(studio, tmp_path):
    from test_llm_loops import Relay, dechunk
    port, log = studio
    relay = Relay(tmp_path, port, start(llm_models="openai/gpt-oss-20b"))
    try:
        # The start's own model (loaded: MODEL) and the offered one are its to unload; another model's is not.
        head, body = dechunk(relay.post("/api/v1/models/unload", {"instance_id": "google/gemma-3-27b"}))
        assert b" 403 " in head.split(b"\r\n", 1)[0], head
        error = json.loads(body)["error"]
        assert error["code"] == "model_not_offered" and "google/gemma-3-27b" in error["message"]
        head, _ = dechunk(relay.post("/api/v1/models/unload", {"instance_id": "custom-name"}))
        assert b" 403 " in head.split(b"\r\n", 1)[0]
        assert not [r for r in requests_of(log) if r["path"] == "/api/v1/models/unload"], "nothing reached the server"
        head, _ = dechunk(relay.post("/api/v1/models/unload", {"instance_id": MODEL}))
        assert b" 200 " in head.split(b"\r\n", 1)[0], head
        head, _ = dechunk(relay.post("/api/v1/models/unload", {"instance_id": "openai/gpt-oss-20b:2"}))
        assert b" 404 " in head.split(b"\r\n", 1)[0], "passed on: the server has no such instance"
        assert [r["body"] for r in requests_of(log) if r["path"] == "/api/v1/models/unload"] == \
            [{"instance_id": MODEL}, {"instance_id": "openai/gpt-oss-20b:2"}]
        denied = [r for r in relay.records() if r.get("event") == "denied"]
        assert len(denied) == 2 and denied[0]["rule"] == "(llm-models)"
    finally:
        relay.close()


@pytest.mark.skipif(not shutil.which("openssl"), reason="needs openssl")
def test_a_start_with_every_model_unloads_any(studio, tmp_path):
    from test_llm_loops import Relay, dechunk
    port, log = studio
    relay = Relay(tmp_path, port, start())
    try:
        head, _ = dechunk(relay.post("/api/v1/models/unload", {"instance_id": MODEL}))
        assert b" 200 " in head.split(b"\r\n", 1)[0], head
    finally:
        relay.close()
