"""--hide GLOB (sandbox/hide.py): workspace files such as .env kept out of the agent's sight - an empty read-only file
(or folder) mounted over each match found at the start, by Podman and Bubblewrap alike. The Knostic research's A1
(Claude Code reads .env on its own): with --hide .env the sandbox reads an empty file, and --dry-run lists it.

The permanent regression tests: matches by name and by path; a symlink is never hidden (named), a tracked file is
named (still in Git's objects); the canary .env of a run is never one of them; nothing on the host changes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from live_fakes import Host  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hide = load("sandbox_hide_tests", "sandbox/hide.py")
s = load("agent_sandbox_hide_tests", "sandbox/agent_sandbox.py")
SECRET = "DB_" + "PASSWORD=" + "fixture-value-123"  # assembled at run time


def workspace(root):
    for relative, text in {".env": SECRET + "\n", "app/.env.local": SECRET + "\n", "certs/server.pem": "x\n",
                           "config/secrets/prod.json": "{}", "src/main.py": "print(1)\n", "src/env.py": "x\n",
                           "node_modules/pkg/.env": "x\n"}.items():
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text(text)
    os.symlink(root / ".env", root / "linked.pem")
    return root


@pytest.mark.parametrize("value", ["", "/etc/passwd", "../x", "a/../b", "*", "**", "dir/", " .env", "a\nb"])
def test_a_glob_is_checked(value):
    with pytest.raises(ValueError):
        hide.check_glob(value)


def test_names_and_paths_match(tmp_path):
    ws = workspace(tmp_path / "ws")
    found, notes = hide.find(ws, [".env", ".env.*", "*.pem", "config/secrets/**"])
    assert sorted(found) == [(".env", "file"), ("app/.env.local", "file"), ("certs/server.pem", "file"),
                             ("config/secrets/prod.json", "file")]
    assert notes == ["linked.pem is a symlink: not hidden (a link is never followed)"]
    found, _ = hide.find(ws, ["config/secrets"])
    assert found == [("config/secrets", "dir")]
    found, _ = hide.find(ws, ["**/.env"])
    assert (".env", "file") in found and ("node_modules/pkg/.env", "file") not in found  # node_modules not searched


def test_a_tracked_file_is_named(tmp_path):
    ws = workspace(tmp_path / "ws")
    subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(ws), "add", ".env"], check=True)
    plan = hide.plan([(ws, "/work")], [".env"])
    assert [item["sandbox"] for item in plan["hidden"]] == ["/work/.env"]
    assert any("Git tracks it" in note and "git show HEAD:.env" in note for note in plan["notes"])


def test_bubblewrap_mounts_an_empty_file_over_each_match(tmp_path):
    ws = workspace(tmp_path / "ws")
    session = tmp_path / "session"
    session.mkdir()
    a = s.parse_args(["codex", "--backend", "bwrap", "--workspace", str(ws), "--hide", ".env", "--hide", "secrets"])
    said = []
    s.plan_hidden(a, session, said.append)
    cmd = s.bwrap_command(a, session, tmp_path / "home")
    work = cmd.index("/work")
    over = cmd.index("/work/.env")
    assert over > work and cmd[over - 2:over] == ["--ro-bind", str(session / "hidden/file")]
    assert cmd[cmd.index("/work/config/secrets") - 1] == str(session / "hidden/dir")
    assert (session / "hidden/file").read_bytes() == b"" and not os.access(session / "hidden/file", os.W_OK) \
        or os.geteuid() == 0
    assert "hidden from the agent (--hide): /work/.env, /work/config/secrets" in said[0]
    assert (ws / ".env").read_text() == SECRET + "\n"


def test_a1_dry_run_lists_and_podman_mounts_over_dot_env(tmp_path):
    """The research's A1 test (a), through the real start: --dry-run lists what is hidden; the sandbox's mounts put an
    empty read-only file over /work/.env, after the workspace."""
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    workspace(host.workspace)
    plan = host.run("claude", "--dry-run", "--hide", ".env", "--hide", ".env.*")
    assert plan.returncode == 0, plan.stderr
    assert [item["sandbox"] for item in json.loads(plan.stdout)["hidden"]] == ["/work/.env", "/work/app/.env.local"]
    started = host.run("claude", "--no-tmux", "--no-checkpoint", "--canaries", "workspace", "--hide", ".env",
                       "--hide", "*.pem", env={"FAKE_PODMAN_RUN": "0:0", "FAKE_PODMAN_KEEP": str(tmp_path / "kept")})
    assert started.returncode == 0, started.stderr
    assert "hidden from the agent (--hide): /work/.env, /work/certs/server.pem" in started.stderr
    assert "linked.pem is a symlink: not hidden" in started.stderr
    run_call = next(call for call in host.calls() if call[:1] == ["run"] and "--pod" in call)
    mounts = [run_call[i + 1] for i, value in enumerate(run_call) if value == "--mount"]
    work = next(i for i, m in enumerate(mounts) if ",dst=/work," in m)
    over = next(i for i, m in enumerate(mounts) if ",dst=/work/.env," in m)
    assert over > work and mounts[over].startswith("type=bind,src=") and ",ro," in mounts[over]
    source = mounts[over].split("src=", 1)[1].split(",dst=", 1)[0]
    assert source.endswith("/hidden/file")
    assert (tmp_path / "kept/session/hidden/file").read_bytes() == b""
    assert (host.workspace / ".env").read_text() == SECRET + "\n"  # the host's file is untouched


def test_the_canary_env_is_never_hidden(tmp_path):
    """No .env in the workspace: the run's canary .env is planted after the plan, so --hide .env does not cover it."""
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    (host.workspace / "main.py").write_text("x\n")
    started = host.run("claude", "--no-tmux", "--no-checkpoint", "--canaries", "workspace", "--hide", ".env",
                       env={"FAKE_PODMAN_RUN": "0:0"})
    assert started.returncode == 0, started.stderr
    assert "--hide .env: nothing matches in the workspaces now" in started.stderr
    run_call = next(call for call in host.calls() if call[:1] == ["run"] and "--pod" in call)
    assert not any(",dst=/work/.env," in value for value in run_call)
def test_hide_suggestions_are_the_completions():
    completion = load("completion_hide_tests", "sandbox/completion.py")
    assert completion.hide_globs() == list(hide.SUGGESTED)


def test_the_wizard_offers_and_takes_it():
    """The wizard names --hide with the suggested globs before its free options line, and a project file may carry
    it (its "options")."""
    wizard = load("wizard_hide_tests", "sandbox/wizard.py")
    assert wizard.check_free_options("claude", "--hide .env --hide '*.pem'") == ["--hide", ".env", "--hide", "*.pem"]
    with pytest.raises(ValueError):
        wizard.check_free_options("claude", "--hide /etc/shadow")
    lines, answers = [], iter(["n", "n", "", "", "", "", ""])
    prompts = type("P", (), {"ask": lambda self, q, d=None: next(answers), "edit": lambda self, q, d="": "",
                             "yes": lambda self, q, d=False: next(answers) == "y", "out": lambda self, t: lines.append(t)})()
    wizard.ask_extra(prompts, {"agent": "codex", "gui": False}, Path("/tmp/proj"), Path("/tmp/state"))
    assert any("--hide .env" in line for line in lines)


def test_the_short_option(tmp_path):
    host = Host(tmp_path / "host")
    host.add_image("localhost/claude-sandbox:latest")
    workspace(host.workspace)
    plan = host.run("claude", "--dry-run", "-hf", ".env")
    assert plan.returncode == 0, plan.stderr
    assert [item["sandbox"] for item in json.loads(plan.stdout)["hidden"]] == ["/work/.env"]
