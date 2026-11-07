"""sandbox-ai defaults (sandbox/defaults_cmd.py): defaults.json without editing it; each value checked by the
launcher itself before the file is replaced, the other keys kept, a broken file never overwritten. The web
interface and the app set other hosts' defaults (the agents' telemetry among them) through it."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("defaults_for_tests", ROOT / "sandbox" / "defaults_cmd.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path / "config/sandbox-ai/defaults.json"


def run(*args):
    lines = []
    return d.cli(list(args), out=lines.append), lines


def test_show_set_unset(config):
    status, lines = run("show", "--json")
    shown = json.loads(lines[0])
    assert status == 0 and shown["values"] == {} and "telemetry" in shown["keys"]
    assert shown["telemetry_levels"] == ["off", "strict", "on"] and "paranoid" in shown["modes"]
    assert shown["canary_default"] == ["credentials", "home"]
    assert run("set", "telemetry", "strict")[0] == 0
    assert run("set", "mode", "high")[0] == 0
    assert run("set", "canaries", '["credentials"]')[0] == 0
    assert run("set", "keep_days", "90")[0] == 0
    assert json.loads(config.read_text()) == {"version": 1, "telemetry": "strict", "mode": "high",
                                              "canaries": ["credentials"], "keep_days": 90}
    assert oct(config.stat().st_mode & 0o777) == "0o600"
    assert run("unset", "mode")[0] == 0 and "mode" not in json.loads(config.read_text())
    assert run("unset", "mode")[1] == ["mode was not set"]
    assert not list(config.parent.glob(".defaults.json.*")), "no checking copy left behind"


@pytest.mark.parametrize("key, value, why", [
    ("telemetry", "maybe", "telemetry is one of"), ("mode", "extreme", "security mode"),
    ("canaries", '["passwords"]', "categories"), ("keep_days", "0", "keep_days"), ("nothing", "1", "no key"),
    ("subnet", "8.8.8.0/24", "subnet"),
])
def test_what_the_launcher_refuses_is_never_written(config, key, value, why):
    assert run("set", "telemetry", "strict")[0] == 0
    before = config.read_text()
    with pytest.raises(ValueError, match=why):
        d.cli(["set", key, value])
    assert config.read_text() == before


def test_a_broken_file_is_left_alone(config):
    config.parent.mkdir(parents=True)
    config.write_text("{broken")
    with pytest.raises(ValueError, match="fix or remove it first"):
        d.cli(["set", "telemetry", "off"])
    assert config.read_text() == "{broken"


def test_the_command_line(config):
    env = {**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"}
    main = ROOT / "sandbox" / "main.py"
    done = subprocess.run([sys.executable, "-I", str(main), "defaults", "set", "telemetry", "on"], capture_output=True,
                          text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    done = subprocess.run([sys.executable, "-I", str(main), "defaults", "set", "telemetry", "loud"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 2 and "telemetry is one of" in done.stderr
    done = subprocess.run([sys.executable, "-I", str(main), "defaults"], capture_output=True, text=True, env=env,
                          timeout=120)
    assert '"on"' in done.stdout and "telemetry" in done.stdout
