"""The Sandboxes page's Memory column: what the launcher's memory watch writes for a running sandbox
(memory.json in its session directory), numbers only."""
import json

from sandbox_ai_app import backend


def instance(tmp_path, data, host="local"):
    session = tmp_path / "session"
    session.mkdir(exist_ok=True)
    if data is not None:
        (session / "memory.json").write_text(data if isinstance(data, str) else json.dumps(data))
    return backend.Instance("run", "r", "r", record={"session_dir": str(session)}, state="running", host=host)


def test_the_share_comes_from_the_watchs_memory_json(tmp_path):
    found = backend.memory_share(instance(tmp_path, {"used_bytes": 7 << 30, "limit_bytes": 8 << 30,
                                                      "advice": "\x1b[31m anything"}))
    assert found == (87.5, 7 << 30, 8 << 30)


def test_nothing_is_made_up(tmp_path):
    for data in (None, "not json", {"used_bytes": "7g", "limit_bytes": 8}, {"used_bytes": 1, "limit_bytes": 0},
                 {"used_bytes": -1, "limit_bytes": 8}, ["x"]):
        assert backend.memory_share(instance(tmp_path, data)) is None, data
    assert backend.memory_share(instance(tmp_path, {"used_bytes": 1, "limit_bytes": 2}, host="ct1")) is None
    assert backend.memory_share(backend.Instance("run", "r", "r")) is None  # no session directory known
