"""sandbox-ai doctor --json: one object per check, with the area of the table it belongs to (the app groups by it)."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_ai_doctor_json", ROOT / "sandbox/doctor.py")
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)


def test_json_has_status_check_detail_hint_and_group(monkeypatch, capsys):
    results = [("ok", "Python", "3.12", ""), ("warn", "Image claude", "missing", "sandbox-ai build claude"),
               ("fail", "Login codex", "no login", "sandbox-ai login codex"), ("ok", "Project configuration", "-", "")]
    monkeypatch.setattr(d, "checks", lambda backend, podman_url=None: results)
    assert d.cli(["--json"]) == 1, "a failed check: status 1"
    out = json.loads(capsys.readouterr().out)
    assert out[1] == {"status": "warn", "check": "Image claude", "detail": "missing", "hint": "sandbox-ai build claude",
                      "group": "Engine"}
    assert [entry["group"] for entry in out] == ["System", "Engine", "Agents", "Project"]
    monkeypatch.setattr(d, "checks", lambda backend, podman_url=None: results[:1])
    assert d.cli(["--json"]) == 0
