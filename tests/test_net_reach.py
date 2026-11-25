"""What a sandbox may reach, and why: agent_sandbox.reach() (each allowance of a start with its source and scope), its
record in run.json and the plan, and `sandbox-ai net show`, which lists it beside what was allowed during the run."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

SPEC = importlib.util.spec_from_file_location("sandbox_net_reach", ROOT / "sandbox" / "net.py")
net = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(net)
L = net.launcher()


def parsed(argv, project=None, project_path="/p/.sandbox-ai.json"):
    """The options of a start as launch_once keeps them: the start's own tokens, the project file's document."""
    a = L.parse_args(argv)
    a.start_tokens = list(argv)
    a.project_document = {"options": list(project)} if project is not None else None
    a.project_config = project_path if project is not None else None
    return a


def by_what(entries):
    return {entry["what"]: entry for entry in entries}


def test_reach_names_the_source_of_every_allowance(tmp_path):
    a = parsed(["claude", "--workspace", str(tmp_path), "--profile", "development", "-A", "example.org",
                "--allow-tcp", "192.0.2.4:22", "--dns", "--allow-host-port", "3000"],
               project=["--allow-host=proj.example.org", "--allow-pypi", "--allow", "198.51.100.0/24"])
    # (what the project file names joins the options as merge_config does it)
    a.allow_host.append("proj.example.org")
    a.presets.append("pypi")
    a.allow.append("198.51.100.0/24")
    found = by_what(L.reach(a))
    assert found["claude"]["source"] == "provider" and "api.anthropic.com" in found["claude"]["hosts"]
    assert found["--frontier"]["source"] == "profile" and found["--frontier"]["detail"] == "development"
    assert found["--allow-github"]["source"] == "profile"
    assert found["--allow-pypi"] == {"kind": "preset", "what": "--allow-pypi", "source": "project",
                                     "detail": "/p/.sandbox-ai.json", "hosts": ["pypi.org", "files.pythonhosted.org"],
                                     "scope": "every start of the project"}
    assert found["proj.example.org"]["source"] == "project" and found["proj.example.org"]["kind"] == "https"
    assert found["example.org"] == {"kind": "https", "what": "example.org", "source": "option"}
    assert found["192.0.2.4:22"]["kind"] == "tcp" and found["192.0.2.4:22"]["source"] == "option"
    assert found["198.51.100.0/24"]["kind"] == "network" and found["198.51.100.0/24"]["source"] == "project"
    assert found["3000"]["kind"] == "host-port"
    assert found["1.1.1.1, 8.8.8.8"]["kind"] == "dns" and found["1.1.1.1, 8.8.8.8"]["source"] == "option"
    # Each name is listed once: under its provider or preset, never again on its own.
    names = [h for e in found.values() for h in e.get("hosts", [])] + [e["what"] for e in found.values()
                                                                        if e["kind"] == "https"]
    assert len(names) == len(set(names))


def test_reach_tells_what_other_options_and_approvals_opened(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_TOKEN_X", "abcdefghijklmnop1234567890")
    a = parsed(["codex", "--workspace", str(tmp_path), "--git-token-env", "https://git.example.net=GIT_TOKEN_X",
                "--git-token-env", "https://github.com=GIT_TOKEN_X"])
    a.remembered_hosts, a.approved_tcp = ["approved.example.org"], ["192.0.2.7:443"]
    a.allow_host.append("approved.example.org")
    a.allow_tcp.append("192.0.2.7:443")
    found = by_what(L.reach(a))
    assert found["git.example.net"]["source"] == "git-login"
    assert found["--allow-github"]["source"] == "git-login"  # a login at github.com opens its preset
    assert found["approved.example.org"]["source"] == "approved"
    assert found["approved.example.org"]["scope"] == "every start of the project"
    assert found["192.0.2.7:443"]["source"] == "approved"


def test_reach_of_an_offline_and_an_open_start(tmp_path):
    assert L.reach(parsed(["claude", "--workspace", str(tmp_path), "--offline"])) == [
        {"kind": "nothing", "what": "--offline", "source": "option"}]
    found = by_what(L.reach(parsed(["claude", "--workspace", str(tmp_path), "--profile", "egress-all"])))
    assert found["--egress-all-allow"] == {"kind": "everything", "what": "--egress-all-allow", "source": "profile",
                                           "detail": "egress-all"}
    assert found["1.1.1.1, 8.8.8.8"]["source"] == "profile"  # its real DNS comes with it


def test_recorded_reach_never_stops_a_start(monkeypatch, tmp_path):
    def broken(_a):
        raise KeyError("x")
    monkeypatch.setattr(L, "reach", broken)
    assert L.recorded_reach(parsed(["claude", "--workspace", str(tmp_path)])) == []


def test_register_run_and_the_plan_record_it(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    a = parsed(["claude", "--workspace", str(tmp_path / "app"), "--allow-github"])
    L.validate(a)
    record = json.loads((L.register_run(a, "claude-1234abcd", tmp_path / "session") / "run.json").read_text())
    assert {"kind": "preset", "what": "--allow-github", "source": "option",
            "hosts": L.PRESETS["github"]} in record["reach"]


def test_the_dry_runs_plan_lists_it(tmp_path):
    (tmp_path / "w").mkdir()
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg"), "TMPDIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "main.py"), "claude", "--dry-run",
                             "--no-config",
                             "--allow-npm", "-A", "example.org"], cwd=tmp_path / "w", env=env, capture_output=True,
                            text=True, timeout=120, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr
    found = by_what(json.loads(result.stdout)["reach"])
    assert found["--allow-npm"]["source"] == "option" and found["example.org"]["source"] == "option"
    assert found["claude"]["source"] == "provider"


STARTED = [{"kind": "preset", "what": "codex", "source": "provider", "detail": "codex",
            "hosts": ["api.openai.com", "chatgpt.com"]},
           {"kind": "preset", "what": "--allow-pypi", "source": "project", "detail": "/p/.sandbox-ai.json",
            "hosts": ["pypi.org"], "scope": "every start of the project"},
           {"kind": "tcp", "what": "192.0.2.4:22", "source": "option"}]


def test_net_show_lists_the_start_with_its_sources_then_the_run(monkeypatch):
    monkeypatch.setenv("COLUMNS", "200")  # (the last column wraps at the terminal's width)
    lines = []
    net.show({}, lines.append, run="codex-1", started=STARTED)
    text = "\n".join(lines)
    assert "codex-1: what it may reach from its start" in lines[0]
    assert lines[1].split()[:2] == ["KIND", "SOURCE"]
    assert "the agent's provider (codex)" in text and "api.openai.com, chatgpt.com" in text
    assert "the project file (/p/.sandbox-ai.json)" in text and "every start of the project" in text
    assert "--allow-pypi: pypi.org" in text
    assert any(line.split()[:1] == ["TCP"] and "192.0.2.4:22" in line for line in lines)
    assert text.index("from its start") < text.index("allowed during this run")
    assert "not recorded" not in text
    lines = []
    net.show({}, lines.append, run="codex-1", as_json=True, started=STARTED)
    assert json.loads("\n".join(lines))["started"] == STARTED


def test_net_show_of_an_older_run_says_its_start_is_not_recorded():
    lines = []
    net.show({}, lines.append, run="codex-1")
    assert "not recorded" in lines[-1] and "what it may reach from its start" not in "\n".join(lines)
    lines = []
    net.show({}, lines.append, run="codex-1", as_json=True)
    assert "started" not in json.loads("\n".join(lines))


@pytest.mark.parametrize("value", [None, "x", [1, "y", {"kind": "https", "what": "a.example", "source": "zzz"}]])
def test_started_rows_take_what_run_json_holds_as_it_is(value):
    rows = net.started_rows(value if isinstance(value, list) else [])
    assert all(len(row) == 4 for row in rows)
