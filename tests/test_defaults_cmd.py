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
    assert shown["telemetry_levels"] == ["off", "strict"] and "paranoid" in shown["modes"]
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
    ("canaries", '["passwords"]', "categories"), ("keep_days", "-1", "keep_days"), ("nothing", "1", "no key"),
    ("subnet", "8.8.8.0/24", "subnet"), ("hide_secrets", '"yes"', "hide_secrets is true or false"),
])
def test_what_the_launcher_refuses_is_never_written(config, key, value, why):
    assert run("set", "telemetry", "strict")[0] == 0
    before = config.read_text()
    with pytest.raises(ValueError, match=why) as refused:
        d.cli(["set", key, value])
    assert config.read_text() == before
    assert ".check" not in str(refused.value)  # review 5 (correctness L5): never the name of the checking copy


def test_hide_secrets_is_a_default(config):
    """Settings > Hide logins and keys in file views: hide_secrets true leaves them out of sandbox-ai session files
    and memory (the app's and the web interface's file views); without it they are listed and marked."""
    assert run("set", "hide_secrets", "true")[0] == 0
    assert json.loads(config.read_text())["hide_secrets"] is True
    assert "hide_secrets" in json.loads(run("show", "--json")[1][0])["keys"]
    assert run("unset", "hide_secrets")[0] == 0 and "hide_secrets" not in json.loads(config.read_text())


def test_a_broken_file_is_left_alone(config):
    config.parent.mkdir(parents=True)
    config.write_text("{broken")
    with pytest.raises(ValueError, match="fix or remove it first"):
        d.cli(["set", "telemetry", "off"])
    assert config.read_text() == "{broken"


def test_the_command_line(config):
    env = {**os.environ, "SANDBOX_AI_NO_DEPRECATION": "1"}
    main = ROOT / "sandbox" / "main.py"
    done = subprocess.run([sys.executable, "-I", str(main), "defaults", "set", "telemetry", "strict"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    done = subprocess.run([sys.executable, "-I", str(main), "defaults", "set", "telemetry", "on"], capture_output=True,
                          text=True, env=env, timeout=120)
    assert done.returncode == 2 and "telemetry is always off" in done.stderr, "on is gone: never written"
    done = subprocess.run([sys.executable, "-I", str(main), "defaults", "set", "telemetry", "loud"],
                          capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 2 and "telemetry is one of" in done.stderr
    done = subprocess.run([sys.executable, "-I", str(main), "defaults"], capture_output=True, text=True, env=env,
                          timeout=120)
    assert '"strict"' in done.stdout and "telemetry" in done.stdout



def test_two_changes_at_once_keep_both(config, monkeypatch):
    """Review round 2: `defaults set` read the file, checked the value (a launcher run: seconds) and wrote back what
    it had read - a second `set` meanwhile (the app, the web interface, the CLI) was lost (a security mode set, then
    dropped by the other's write). Both changes now hold the file's lock from reading to writing."""
    import threading
    assert run("set", "telemetry", "strict")[0] == 0
    original, inside = d.check, threading.Event()

    def slow_check(document, path):
        inside.set()
        threading.Event().wait(0.5)  # the other command reads meanwhile
        return original(document, path)

    monkeypatch.setattr(d, "check", slow_check)
    first = threading.Thread(target=run, args=("set", "mode", "high"))
    first.start()
    assert inside.wait(30)
    monkeypatch.setattr(d, "check", original)
    assert run("set", "keep_days", "90")[0] == 0
    first.join(60)
    assert json.loads(config.read_text()) == {"version": 1, "telemetry": "strict", "mode": "high", "keep_days": 90}


def test_an_old_on_in_the_file_is_ignored_and_never_written(config, capsys):
    """Telemetry is always off (0.1.95): `set telemetry on` is refused; a file that still says on is read (and its
    other keys changed) as if it did not."""
    with pytest.raises(ValueError, match="telemetry is always off"):
        d.cli(["set", "telemetry", "on"])
    assert not config.exists()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"version": 1, "telemetry": "on"}))
    assert run("set", "mode", "high")[0] == 0
    assert json.loads(config.read_text())["mode"] == "high"
    assert run("unset", "telemetry")[0] == 0 and "telemetry" not in json.loads(config.read_text())


def test_the_update_keys_are_shown_but_set_by_update_mode(config):
    """update_mode, update_window and update_source belong to `sandbox-ai update mode`, which makes or removes the
    update timer with them: `defaults set` names it; a file with a wrong value is refused by the launcher's check."""
    import pytest as _pytest
    with _pytest.raises(ValueError, match="set by sandbox-ai update mode"):
        run("set", "update_mode", "auto")
    with _pytest.raises(ValueError, match="set by sandbox-ai update mode"):
        run("unset", "update_window")
    assert "update_mode" in json.loads(run("show", "--json")[1][0])["keys"]
    document, path = d.read()
    with _pytest.raises(ValueError, match="update_mode is one of off, notify, download, auto"):
        d.check({**document, "update_mode": "sometimes"}, path)
