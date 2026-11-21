"""`codex login` in a sandbox runs Codex's device-code login (sandbox/telemetry_guard.py: codex_login_args, the same
decision in the generated JavaScript every `codex` in the image imports first).

Regression (2026-11-17, live): `codex login` inside a sandbox printed
https://auth.openai.com/oauth/authorize?...&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback - a browser
outside the pod cannot reach the pod's 127.0.0.1:1455, so the login never finished. `sandbox-ai login codex` used
--device-auth already; a plain `codex login` typed in the sandbox (or run by an agent) did not. Codex 0.169.0's
`codex login` takes --device-auth, --with-api-key, --with-access-token (--api-key is refused by Codex itself) and the
subcommand status; its top-level -c/--config, --enable, --disable, -p/--profile and the like may come first."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("telemetry_guard_login_tests", ROOT / "sandbox/telemetry_guard.py")
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)
NODE = shutil.which("node")

CASES = [
    (["login"], ["login", "--device-auth"]),
    (["-c", "model=o3", "login"], ["-c", "model=o3", "login", "--device-auth"]),
    (["--config=model=o3", "login", "-c", "x=1"], ["--config=model=o3", "login", "--device-auth", "-c", "x=1"]),
    (["--profile", "work", "login"], ["--profile", "work", "login", "--device-auth"]),
    (["--enable", "foo", "--no-alt-screen", "login"], ["--enable", "foo", "--no-alt-screen", "login", "--device-auth"]),
    # Kept as they are: a login of its own kind, the status, help, other commands, a prompt that says "login".
    (["login", "--device-auth"], None),
    (["login", "--with-api-key"], None),
    (["login", "--with-access-token"], None),
    (["login", "--api-key", "sk-x"], None),
    (["login", "status"], None),
    (["login", "help"], None),
    (["login", "--help"], None),
    (["login", "-h"], None),
    (["logout"], None),
    ([], None),
    (["exec", "login"], None),
    (["-m", "login"], None),          # "login" is the model's name here
    (["--", "login"], None),
    (["fix the login page"], None),
]


@pytest.mark.parametrize("args, expected", CASES)
def test_a_plain_codex_login_becomes_the_device_code_login(args, expected):
    assert guard.codex_login_args(args, {}) == expected


def test_the_browser_login_may_be_kept_on_purpose():
    # With the callback relayed by sandbox-ai (the human opens the link from the approvals' browser question).
    assert guard.codex_login_args(["login"], {"SANDBOX_AI_LOGIN_CALLBACK": "1"}) is None
    assert guard.codex_login_args(["login"], {"SANDBOX_AI_LOGIN_CALLBACK": "0"}) == ["login", "--device-auth"]


def test_the_note_says_why_in_one_line():
    note = guard.CODEX_LOGIN_NOTE
    assert "\n" not in note.rstrip("\n") and note.endswith("\n")
    assert "--device-auth" in note and "127.0.0.1:1455" in note and "SANDBOX_AI_LOGIN_CALLBACK=1" in note


def node_codex(tmp_path, args, environ=None):
    """Codex's entry stand-in that imports the generated guard (as the image's bin/codex.js does) and prints the
    arguments Codex's binary would get."""
    (tmp_path / "telemetry-guard.mjs").write_text(guard.javascript())
    entry = tmp_path / "codex-entry.mjs"
    entry.write_text("#!/usr/bin/env node\n" + guard.import_line("codex", str(tmp_path / "telemetry-guard.mjs"))
                     + "\nconsole.log(JSON.stringify(process.argv.slice(2)));\n")
    return subprocess.run([NODE, str(entry), *args], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"], **(environ or {})})


@pytest.mark.skipif(not NODE, reason="node")
@pytest.mark.parametrize("args, expected", CASES)
def test_the_javascript_guard_decides_the_same(tmp_path, args, expected):
    done = node_codex(tmp_path, args)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == (expected if expected is not None else args)
    assert done.stderr == (guard.CODEX_LOGIN_NOTE if expected is not None else "")


@pytest.mark.skipif(not NODE, reason="node")
def test_the_javascript_guard_keeps_the_browser_login_on_purpose(tmp_path):
    done = node_codex(tmp_path, ["login"], {"SANDBOX_AI_LOGIN_CALLBACK": "1"})
    assert done.returncode == 0 and json.loads(done.stdout) == ["login"] and done.stderr == ""


@pytest.mark.skipif(not NODE, reason="node")
def test_other_node_agents_are_left_alone(tmp_path):
    (tmp_path / "telemetry-guard.mjs").write_text(guard.javascript())
    entry = tmp_path / "qwen-entry.mjs"
    entry.write_text("#!/usr/bin/env node\n" + guard.import_line("qwen", str(tmp_path / "telemetry-guard.mjs"))
                     + "\nconsole.log(JSON.stringify(process.argv.slice(2)));\n")
    done = subprocess.run([NODE, str(entry), "login"], capture_output=True, text=True, timeout=60,
                          env={"PATH": os.environ["PATH"]})
    assert json.loads(done.stdout) == ["login"] and done.stderr == ""


def test_sandbox_ai_login_codex_still_asks_for_the_device_login_itself():
    spec = importlib.util.spec_from_file_location("login_for_device_tests", ROOT / "sandbox/login.py")
    login = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(login)
    assert login.LOGIN_ARGS["codex"] == ["login", "--device-auth"]
    assert guard.codex_login_args(login.LOGIN_ARGS["codex"], {}) is None
