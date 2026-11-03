"""Happy/sad/edge contracts and an end-to-end run for the project wizard."""
import http.server
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sandbox_wizard_test", ROOT / "sandbox/wizard.py")
w = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(w)

OLLAMA = {"/api/tags": {"models": [
              {"name": "llama3.2:3b", "size": 2019393189, "details": {"parameter_size": "3.2B", "quantization_level": "Q4_K_M"}},
              {"name": "qwen2.5-coder:14b", "size": 8988124069, "details": {"parameter_size": "14.8B", "quantization_level": "Q4_K_M"}},
              {"name": "all-minilm", "size": 45960996, "details": {}}]},
          "/api/ps": {"models": [{"name": "qwen2.5-coder:14b"}]}}
LMSTUDIO = {"/api/v0/models": {"data": [
                {"id": "qwen2.5-7b-instruct", "type": "llm", "state": "loaded", "quantization": "Q4_K_M"},
                {"id": "text-embedding-nomic", "type": "embeddings", "state": "not-loaded"},
                {"id": "gemma-3-12b", "type": "vlm", "state": "not-loaded", "quantization": "Q8_0"}]}}
VLLM = {"/v1/models": {"data": [{"id": "meta-llama/Llama-3.1-8B-Instruct", "max_model_len": 32768}]}}


@pytest.fixture(params=["happy", "sad", "edge"])
def scenario(request):
    return request.param


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))  # kits of whoever runs the tests
    # A ~/.git-credentials of whoever runs the tests would add a question.
    monkeypatch.setattr(w, "git_credentials_path", lambda: tmp_path / "no-home" / ".git-credentials")


def fake_fetch(routes, seen=None):
    def fetch(url, timeout=5):
        if seen is not None:
            seen.append(url)
        path = "/" + url.split("/", 3)[3]
        if path not in routes:
            raise OSError(f"404 {path}")
        return routes[path]
    return fetch


def test_discover(scenario):
    if scenario == "sad":
        with pytest.raises(ValueError, match="Cannot reach ollama at 10.0.0.9:11434"):
            w.discover("ollama", "10.0.0.9", 11434, fetch=fake_fetch({}))
        return
    if scenario == "happy":
        seen = []
        models = w.discover("ollama", "10.0.0.9", 11434, fetch=fake_fetch(OLLAMA, seen))
        assert seen == ["http://10.0.0.9:11434/api/tags", "http://10.0.0.9:11434/api/ps"]
        assert [(m["name"], m["size"], m["loaded"]) for m in models] == [
            ("llama3.2:3b", 2019393189, False), ("qwen2.5-coder:14b", 8988124069, True), ("all-minilm", 45960996, False)]
        assert models[0]["details"] == "3.2B Q4_K_M"
        lm = w.discover("lmstudio", "10.0.0.9", 1234, fetch=fake_fetch(LMSTUDIO))
        assert [(m["name"], m["loaded"]) for m in lm] == [("qwen2.5-7b-instruct", True), ("gemma-3-12b", False)]
        vl = w.discover("vllm", "10.0.0.9", 8000, fetch=fake_fetch(VLLM))
        assert vl == [{"name": "meta-llama/Llama-3.1-8B-Instruct", "size": None, "loaded": True,
                       "details": "max_model_len 32768"}]
    else:
        # Ollama without /api/ps: loaded unknown. Old LM Studio: OpenAI listing only. IPv6 hosts are bracketed.
        seen = []
        models = w.discover("ollama", "fd00::5", 11434, fetch=fake_fetch({"/api/tags": OLLAMA["/api/tags"]}, seen))
        assert seen[0] == "http://[fd00::5]:11434/api/tags" and {m["loaded"] for m in models} == {None}
        lm = w.discover("lmstudio", "10.0.0.9", 1234, fetch=fake_fetch({"/v1/models": {"data": [{"id": "m1"}]}}))
        assert lm == [{"name": "m1", "size": None, "loaded": None, "details": ""}]


def test_sort_models(scenario):
    models = [{"name": "b", "size": 5, "loaded": False}, {"name": "a", "size": None, "loaded": True},
              {"name": "c", "size": 9, "loaded": None}]
    if scenario == "sad":
        with pytest.raises(ValueError, match="name, size or loaded"):
            w.sort_models(models, "speed")
    elif scenario == "happy":
        assert [m["name"] for m in w.sort_models(models, "name")] == ["a", "b", "c"]
        assert [m["name"] for m in w.sort_models(models, "size")] == ["c", "b", "a"]
    else:
        assert [m["name"] for m in w.sort_models(models, "loaded")] == ["a", "b", "c"]
        assert w.sort_models([], "size") == []


def test_format_size(scenario):
    value = {"happy": 2019393189, "sad": None, "edge": 999}[scenario]
    assert w.format_size(value) == {"happy": "2.0 GB", "sad": "-", "edge": "999 B"}[scenario]


def test_model_table(scenario):
    models = [] if scenario == "sad" else [{"name": "x" * (60 if scenario == "edge" else 3), "size": 2 ** 30,
                                            "loaded": True, "details": "8B"}]
    lines = w.model_table(models)
    assert lines[0].split() == ["#", "NAME", "SIZE", "LOADED", "DETAILS"]
    if scenario == "sad":
        assert lines[1] == "(no models found)"
    else:
        assert lines[1].split()[:4] == ["1", models[0]["name"], "1.1", "GB"] and "yes" in lines[1]


def test_opencode_config(scenario):
    host = {"happy": "192.168.1.20", "sad": "192.168.1.20", "edge": "fd00::5"}[scenario]
    kind = {"happy": "ollama", "sad": "vllm", "edge": "lmstudio"}[scenario]
    config = w.opencode_config(kind, host, 1234, "my/model:1")
    provider = config["provider"][kind]
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["options"]["baseURL"] == ("http://[fd00::5]:1234/v1" if scenario == "edge" else "http://192.168.1.20:1234/v1")
    assert provider["models"] == {"my/model:1": {"name": "my/model:1"}}
    assert config["model"] == config["small_model"] == f"{kind}/my/model:1"
    assert config["enabled_providers"] == [kind] and config["share"] == "disabled" and config["autoupdate"] is False


def test_build_document(scenario, tmp_path):
    answers = {"agent": "opencode", "backend": "podman", "workspaces": [], "network": "default",
               "danger": False, "gui": False, "tmux": True, "log_dir": None, "local": None}
    if scenario == "happy":
        answers.update(local={"config": str(tmp_path / "oc.json"), "host": "192.168.1.20", "port": 11434})
        doc = w.build_document(answers)
        assert doc["options"] == ["--local-config", str(tmp_path / "oc.json"), "--allow-tcp", "192.168.1.20:11434"]
    elif scenario == "sad":
        assert w.build_document(answers) == {"version": 1, "agent": "opencode", "options": [], "workspaces": [],
                                             "description": "Created by sandbox-wizard"}
    else:
        answers.update(agent="claude", backend="bwrap", workspaces=["/x"], network="frontier", danger=True, gui=True,
                       tmux=False, log_dir="/logs")
        doc = w.build_document(answers)
        assert doc["options"] == ["--backend", "bwrap", "--profile", "frontier", "--dangerously-skip-permissions",
                                  "--gui", "--no-tmux", "--log-dir", "/logs"]
        assert doc["workspaces"] == ["/x"]


def scripted(answers):
    queue = list(answers)
    def prompt(text):
        assert queue, f"unexpected question: {text}"
        return queue.pop(0)
    return prompt


def test_run_writes_trusted_local_llm_project(tmp_path):
    project = tmp_path / "proj"
    out = []
    path = w.run(prompt=scripted([str(project), "", "opencode", "podman", "y", "ollama", "192.168.1.20", "", "size", "2",
                                  "n", "n", "", "n", "n", "n", "n", "y"]),
                 out=out.append, fetch=fake_fetch(OLLAMA))
    assert path == project / ".sandbox-ai.json"
    document = json.loads(path.read_text())
    local = w.local_config_path(project)
    assert project not in local.parents  # outside the workspace, so the agent cannot swap it
    assert document["options"] == ["--local-config", str(local), "--allow-tcp", "192.168.1.20:11434"]
    # Sorted by size, entry 2 is llama3.2:3b.
    assert json.loads(local.read_text())["model"] == "ollama/llama3.2:3b"
    launcher = w.launcher()
    args = launcher.parse_args(["opencode", "--workspace", str(project)])
    assert launcher.load_project_config(args)[1] == document
    assert any("./opencode-sandbox --workspace" in line for line in out)


def test_run_handles_loopback_unreachable_and_overwrite(scenario, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    out = []
    if scenario == "sad":
        (project / ".sandbox-ai.json").write_text("{}")
        assert w.run(prompt=scripted([str(project), "cancel"]), out=out.append, fetch=fake_fetch({})) is None
        assert (project / ".sandbox-ai.json").read_text() == "{}"
    elif scenario == "edge":
        # Loopback is unreachable from the sandbox: warned, then replaced by a LAN address.
        path = w.run(prompt=scripted([str(project), "", "opencode", "podman", "y", "vllm", "127.0.0.1", "n",
                                      "10.1.1.1", "", "name", "1", "n", "n", "", "n", "n", "n", "n", "y"]),
                     out=out.append, fetch=fake_fetch(VLLM))
        assert any("loopback" in line for line in out)
        assert "10.1.1.1:8000" in json.loads(path.read_text())["options"]
    else:
        # Unreachable server: the error is shown and the address asked again.
        calls = []
        def fetch(url, timeout=5):
            calls.append(url)
            if "10.9.9.9" in url:
                raise OSError("timed out")
            return fake_fetch(VLLM)(url)
        path = w.run(prompt=scripted([str(project), "", "opencode", "podman", "y", "vllm", "10.9.9.9", "", "10.1.1.1", "",
                                      "loaded", "1", "n", "n", "", "n", "n", "n", "n", "y"]), out=out.append, fetch=fetch)
        assert any("Cannot reach vllm" in line for line in out)
        assert path.is_file()
        # The local OpenCode configuration is written after the check: no false warning about it.
        assert not any("would refuse" in line for line in out)


def test_run_cloud_agent_without_local_llm(tmp_path):
    project = tmp_path / "p"
    path = w.run(prompt=scripted([str(project), f"{tmp_path}/lib", "claude", "bwrap", "frontier", "y", "y", "y", "y", "n", "n", "n", "y"]),
                 out=lambda line: None, fetch=fake_fetch({}))
    document = json.loads(path.read_text())
    assert document["agent"] == "claude" and document["workspaces"] == [f"{tmp_path}/lib"]
    assert document["options"][:6] == ["--backend", "bwrap", "--profile", "frontier", "--dangerously-skip-permissions",
                                       "--gui"]
    # tmux is the launcher's default: only "no" is written down (as --no-tmux).
    assert "--tmux" not in document["options"] and "--no-tmux" not in document["options"]
    assert "--log-dir" in document["options"]


class Prompts:
    """Scripted Prompter that records what it showed; SECRETS answer the hidden token prompts."""
    def __init__(self, answers, secrets=()):
        self.lines = []
        self.p = w.Prompter(scripted(answers), self.lines.append, scripted(secrets))


def git_config(project, text):
    (project / ".git").mkdir(parents=True)
    (project / ".git" / "config").write_text(text)


def test_git_remote_origin(scenario, tmp_path):
    project = tmp_path / "proj"
    if scenario == "happy":
        git_config(project, '[core]\n\tbare = false\n[remote "upstream"]\n\turl = https://example.org/x.git\n'
                            '[remote "origin"]\n\turl = http://alice@localhost:3000/alice/sandbox-ai.git\n')
        assert w.git_remote_origin(project) == "http://alice@localhost:3000"
    elif scenario == "sad":
        assert w.git_remote_origin(project) is None
        git_config(project, '[remote "origin"]\n\turl = git@github.com:owner/repo.git\n')
        assert w.git_remote_origin(project) is None
    else:
        # The repository is agent-writable: a symlinked config or odd URL text is never offered.
        (project / ".git").mkdir(parents=True)
        (tmp_path / "elsewhere").write_text('[remote "origin"]\n\turl = https://example.org/x.git\n')
        (project / ".git" / "config").symlink_to(tmp_path / "elsewhere")
        assert w.git_remote_origin(project) is None
        (project / ".git" / "config").unlink()
        (project / ".git" / "config").write_text('[remote "origin"]\n\turl = https://a\x1b]0;x@host/x\n')
        assert w.git_remote_origin(project) is None


def test_ask_network_extra(scenario):
    answers = {"agent": "claude", "backend": "podman" if scenario != "edge" else "bwrap"}
    if scenario == "happy":
        prompts = Prompts(["github, pypi", "docs.example.org", "192.0.2.10:11434", "192.0.2.0/28", "3000"])
        assert w.ask_network_extra(prompts.p, answers) == [
            "--allow-github", "--allow-pypi", "--allow-host", "docs.example.org",
            "--allow-tcp", "192.0.2.10:11434", "--allow", "192.0.2.0/28", "--allow-host-port", "3000"]
    elif scenario == "sad":
        # Each invalid entry is explained and asked again.
        prompts = Prompts(["github,nope", "", "a..b", "", "host:22", "", "10.0.0.0/33", "", "70000", ""])
        assert w.ask_network_extra(prompts.p, answers) == []
        assert any("Unknown preset 'nope'" in line for line in prompts.lines)
        assert any("hostname" in line.lower() for line in prompts.lines)
        assert len([line for line in prompts.lines if line.startswith("  ")]) == 5
    else:
        # bwrap cannot reach the host loopback, so it is not offered.
        prompts = Prompts(["", "", "", ""])
        assert w.ask_network_extra(prompts.p, answers) == []


GH = "github_pat_" + "A1b2" * 10
GITEA = "0123456789abcdef0123456789abcdef01234567"


def test_ask_git(scenario, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    for name in ("SANDBOX_GIT_TOKEN", "SANDBOX_GH_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CONTRACT_GH", "set")
    if scenario == "happy":
        # Tokens typed (hidden) for github.com and for the project's origin: only file paths reach the options.
        git_config(project, '[remote "origin"]\n\turl = http://localhost:3000/alice/sandbox-ai.git\n')
        pending = []
        prompts = Prompts(["", "enter", "y", "", ""], secrets=[GH, GITEA])
        options = w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project, pending)
        github, gitea = str(w.token_path("github.com")), str(w.token_path("http://localhost:3000"))
        assert options == ["--git-identity", "--git-token-file", f"https://github.com={github}",
                           "--git-token-file", f"http://localhost:3000={gitea}"]
        assert pending == [(github, GH), (gitea, GITEA)]
        assert GH not in "\n".join(prompts.lines) and GITEA not in "\n".join(prompts.lines)
        # A host variable instead: suggested name, note when it is not set.
        prompts = Prompts(["n", "variable", "", "http://localhost:3000", "variable", "", ""])
        options = w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, tmp_path / "none")
        assert options == ["--git-token-env", "https://github.com=SANDBOX_GH_TOKEN",
                           "--git-token-env", "http://localhost:3000=SANDBOX_GIT_TOKEN"]
        assert any("SANDBOX_GIT_TOKEN is not set" in line for line in prompts.lines)
    elif scenario == "sad":
        # Bad names, plain http to a remote host, loopback with bwrap and malformed tokens are refused;
        # the malformed token is never echoed.
        prompts = Prompts(["n", "variable", "GH=x", "CONTRACT_GH", "http://git.example.org", "http://localhost:3000",
                           "https://git.example.org", "enter", ""], secrets=["bad token\nx", ""])
        assert w.ask_git(prompts.p, {"backend": "bwrap", "network": "default"}, project) == [
            "--git-token-env", "https://github.com=CONTRACT_GH"]
        shown = "\n".join(prompts.lines)
        assert "unencrypted" in shown and "Podman" in shown and "does not look like an access token" in shown
        assert "bad token" not in shown
    else:
        # Offline: no tokens are asked for, only the identity.
        prompts = Prompts(["y"])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "offline"}, project) == ["--git-identity"]
        # The agent can rewrite origin: pressing Enter everywhere must never add a token (or a host) for it.
        git_config(project, '[remote "origin"]\n\turl = https://attacker.example/x.git\n')
        prompts = Prompts(["", "", "", ""])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project) == ["--git-identity"]
        # GH_TOKEN of the gh CLI is suggested when it is set.
        monkeypatch.delenv("SANDBOX_GH_TOKEN", raising=False)
        monkeypatch.setenv("GH_TOKEN", GH)
        prompts = Prompts(["n", "variable", "", ""])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, tmp_path / "none") == [
            "--git-token-env", "https://github.com=GH_TOKEN"]


def test_ask_git_credentials(scenario, tmp_path, monkeypatch):
    """~/.git-credentials is offered when it exists, with "no" as default: every login in it reaches the agent."""
    store = tmp_path / "home" / ".git-credentials"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(w, "git_credentials_path", lambda: Path.home() / ".git-credentials")
    project = tmp_path / "none"

    def asked(answers):
        prompts, questions = Prompts(answers), []
        prompt = prompts.p.prompt
        prompts.p.prompt = lambda text: questions.append(text) or prompt(text)
        return prompts, questions
    if scenario == "happy":
        store.parent.mkdir()
        store.write_text("https://u:t@github.com\n")
        prompts, questions = asked(["n", "y", "none", ""])
        options = w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project)
        assert options == ["--git-credentials", "~/.git-credentials"]
        assert any("agent can read every login" in q and q.endswith("[y/N]: ") for q in questions)
        # The path is one the project configuration accepts.
        assert w.launcher().relative_paths(options) == []
        assert w.launcher().relative_paths(["--git-credentials", "git-credentials"]) != []
    elif scenario == "sad":
        # No file: no question; Enter on an existing file keeps it out.
        prompts, questions = asked(["n", "none", ""])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project) == []
        assert not any("--git-credentials" in q for q in questions)
        store.parent.mkdir()
        store.write_text("https://u:t@github.com\n")
        prompts = Prompts(["n", "", "none", ""])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project) == []
    else:
        # Editing: the configured file is the default (also when ~/.git-credentials is missing); offline drops it.
        current = ["--git-credentials", "/private/git-credentials"]
        prompts, questions = asked(["n", "", "none", ""])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project, current=current) == current
        assert any("/private/git-credentials" in q and q.endswith("[Y/n]: ") for q in questions)
        prompts = Prompts(["n"])
        assert w.ask_git(prompts.p, {"backend": "podman", "network": "offline"}, project, current=current) == []
        assert any("Git credentials file are dropped" in line for line in prompts.lines)
        answers, notes = w.answers_from_document({"agent": "claude", "options": current})
        assert answers["git"] == current and notes == []


def test_write_token(scenario, tmp_path):
    path = tmp_path / "cfg" / "sandbox-ai" / "tokens" / "github.com"
    if scenario == "sad":
        path.parent.mkdir(parents=True)
        path.symlink_to(tmp_path / "elsewhere")
        with pytest.raises(ValueError, match="symlink"):
            w.write_token(path, GH)
        assert not (tmp_path / "elsewhere").exists()
        return
    if scenario == "edge":
        path.parent.mkdir(parents=True, mode=0o755)
        path.write_text("old")
        path.chmod(0o644)
    w.write_token(path, GH)
    assert path.read_text() == GH + "\n"
    assert path.stat().st_mode & 0o777 == 0o600 and path.parent.stat().st_mode & 0o777 == 0o700
    # The launcher accepts exactly this file.
    assert w.launcher().read_token_file(path, w.launcher().GIT_TOKEN, "--git-token-file") == GH


def test_token_path(scenario):
    label = {"happy": "github.com", "sad": "http://localhost:3000", "edge": "https://[::1]:8443"}[scenario]
    path = w.token_path(label)
    assert path.parent == w.token_store() and "/" not in path.name and ".." not in path.name
    assert path.name == {"happy": "github.com", "sad": "http-localhost-3000", "edge": "https-1-8443"}[scenario]


def test_ask_extra(scenario, tmp_path):
    project = tmp_path / "proj"
    state = tmp_path / "state"
    source = tmp_path / "data"
    source.mkdir()
    if scenario == "happy":
        prompts = Prompts(["y", "y", f"{source}:/mnt/data:ro", "FOO, BAR", "demo", "1280x720x24", "y",
                           "--allow-gitlab --gui-wayland"])
        options = w.ask_extra(prompts.p, {"agent": "claude", "gui": True}, project, state)
        home, vnc = options[2], options[-3]
        assert options[:2] == ["--workspace-ro", "--state-dir"] and home.startswith(str(state / "sandbox-ai/home"))
        assert options[3:] == ["--mount", f"{source}:/mnt/data:ro", "--env", "FOO", "--env", "BAR", "--mcp", "demo",
                               "--gui-screen", "1280x720x24", "--gui-vnc", vnc, "--allow-gitlab", "--gui-wayland"]
        assert vnc.startswith(str(state / "sandbox-ai/vnc"))
    elif scenario == "sad":
        # Missing sources, bad names, unknown servers and options a project file may not set are asked again.
        prompts = Prompts(["n", "n", "/missing:/mnt/x:ro", "", "1BAD", "", "nope", "", "--session s", "--unknown",
                           "'unclosed", ""])
        assert w.ask_extra(prompts.p, {"agent": "codex", "gui": False}, project, state) == []
        shown = "\n".join(prompts.lines)
        assert "Mount source unavailable" in shown and "Not a variable name" in shown
        assert "Unknown MCP server" in shown and "Not allowed in a project configuration: --session" in shown
        assert "does not accept" in shown and "quotation" in shown
    else:
        # The default screen size adds nothing; a bad size is asked again.
        prompts = Prompts(["n", "n", "", "", "", "big", "", "n", ""])
        assert w.ask_extra(prompts.p, {"agent": "codex", "gui": True}, project, state) == []


def test_run_with_optional_blocks_then_launcher_plan(tmp_path, monkeypatch, capsys):
    project = tmp_path / "proj"
    git_config(project, '[remote "origin"]\n\turl = http://localhost:3000/alice/sandbox-ai.git\n')
    out = []
    path = w.run(prompt=scripted([str(project), "", "claude", "podman", "default", "n", "n", "", "n",
                                  "y", "pypi", "", "", "", "",
                                  "y", "n", "", "y", "variable", "SANDBOX_GITEA_TOKEN", "",
                                  "n", "y"]), out=out.append, fetch=fake_fetch({}))
    document = json.loads(path.read_text())
    assert document["options"] == ["--allow-pypi", "--git-token-env", "http://localhost:3000=SANDBOX_GITEA_TOKEN"]
    # The token is not exported yet: the wizard says the launcher would refuse, and writes anyway.
    assert any("would refuse" in line and "SANDBOX_GITEA_TOKEN" in line for line in out)
    monkeypatch.setenv("SANDBOX_GITEA_TOKEN", "0123456789abcdef0123456789abcdef01234567")
    launcher = w.launcher()
    assert launcher.launch(["claude", "--workspace", str(project), "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["host_ports"] == [3000] and "pypi.org" in plan["https_hosts"]
    assert plan["git_credentials"] == [{"url": "http://localhost:3000", "user": "x-access-token",
                                        "env": "SANDBOX_GITEA_TOKEN"}]


def test_cli_trust_and_models(scenario, tmp_path, capsys, monkeypatch):
    path = tmp_path / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
    if scenario == "happy":
        assert w.cli(["trust", str(path), "--yes"]) == 0
        assert str(path) in w.launcher().trusted_configs()
        assert '"--dns"' in capsys.readouterr().out
    elif scenario == "sad":
        monkeypatch.setattr("builtins.input", lambda text: "n")
        assert w.cli(["trust", str(path)]) == 1
        assert str(path) not in w.launcher().trusted_configs()
    else:
        monkeypatch.setattr(w, "fetch_json", fake_fetch(OLLAMA))
        assert w.cli(["models", "--kind", "ollama", "--host", "10.0.0.9", "--sort", "loaded"]) == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[1].split()[1] == "qwen2.5-coder:14b"


class OllamaHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(OLLAMA.get(self.path, {})).encode()
        self.send_response(200 if self.path in OLLAMA else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_end_to_end_wizard_script_then_launcher_plan(tmp_path):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OllamaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        project = tmp_path / "proj"
        answers = "\n".join([str(project), "", "opencode", "podman", "y", "ollama", "127.0.0.1", "y", str(port),
                             "name", "1", "n", "n", "n", "n", "n", "n", "n", "y"]) + "\n"
        env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
        result = subprocess.run([str(ROOT / "sandbox-wizard")], input=answers, capture_output=True, text=True, env=env,
                                timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "qwen2.5-coder:14b" in result.stdout
        plan = subprocess.run([str(ROOT / "opencode-sandbox"), "--workspace", str(project), "--dry-run"],
                              capture_output=True, text=True, env=env, timeout=60)
        assert plan.returncode == 0, plan.stderr
        plan = json.loads(plan.stdout)
        assert plan["allow_tcp"] == [f"127.0.0.1:{port}"]
        assert plan["https_hosts"] == [] and plan["local_config"].startswith(str(tmp_path / "cfg" / "sandbox-ai" / "opencode"))
        assert plan["project_config"] == str(project / ".sandbox-ai.json")
    finally:
        server.shutdown()


def test_review_trust_hashes_exactly_the_bytes_shown(tmp_path):
    path = tmp_path / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--dns"]}))
    shown = []
    def prompt(text):
        # Content swapped while the human reads the prompt.
        path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--egress-all-allow"]}))
        return "y"
    assert w.trust(path, prompt=prompt, out=shown.append) == 0
    assert any("--dns" in line for line in shown)
    launcher = w.launcher()
    with pytest.raises(ValueError, match="changed since it was trusted"):
        launcher.load_project_config(launcher.parse_args(["codex", "--workspace", str(tmp_path)]))


def test_review_wizard_never_writes_through_symlinks(scenario, tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    link = w.local_config_path(project) if scenario != "sad" else project / ".sandbox-ai.json"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside)
    answers = [str(project), "", "opencode", "podman", "y", "ollama", "192.168.1.20", "", "size", "1", "n", "n", "", "n", "n", "n", "n", "y"]
    with pytest.raises(ValueError, match="symlink"):
        w.run(prompt=scripted(answers), out=lambda line: None, fetch=fake_fetch(OLLAMA))
    assert outside.read_text() == "keep"
    assert str(outside) not in w.launcher().trusted_configs()


def test_review_trust_refuses_hidden_characters(scenario, tmp_path):
    path = tmp_path / ".sandbox-ai.json"
    visible = '{"version": 1, "agent": "codex", "description": "harmless"}'
    hidden = {"happy": visible,
              # A carriage return lets the terminal overwrite the dangerous part with spaces.
              "sad": '{"version": 1, "agent": "codex", "options": ["--egress-all-allow"],\r' + " " * 80 + "}",
              "edge": '{"version": 1, "agent": "codex", "description": "\u202eharmless"}'}[scenario]
    path.write_bytes(hidden.encode() if scenario != "edge" else hidden.replace("\\u202e", "\u202e").encode())
    shown = []
    result = w.trust(path, yes=True, out=shown.append)
    if scenario == "happy":
        assert result == 0 and any('"harmless"' in line for line in shown)
    else:
        assert result == 1 and str(path) not in w.launcher().trusted_configs()
        assert all("\r" not in line and "\u202e" not in line for line in shown)


def test_clean_models(scenario):
    raw = {"happy": [{"name": "llama3.2:3b", "size": 2_000_000_000, "loaded": True, "details": "3B Q4"}],
           "sad": [{"name": "evil\x1b]52;c;cGF5bG9hZA==\x07", "size": 1}, {"name": 5}, {"size": 1}],
           "edge": [{"name": "org/Model-GGUF", "size": "big", "loaded": "yes", "details": "x\x1b[2Jy"},
                    {"name": "m", "size": True, "loaded": None, "details": None}]}[scenario]
    models = w.clean_models(raw)
    if scenario == "happy":
        assert models == raw
    elif scenario == "sad":
        assert models == []
    else:
        assert models == [{"name": "org/Model-GGUF", "size": None, "loaded": None, "details": "x\\x1b[2Jy"},
                          {"name": "m", "size": None, "loaded": None, "details": ""}]


def test_local_config_path(scenario, tmp_path, monkeypatch):
    project = {"happy": tmp_path / "app", "sad": Path("/"), "edge": tmp_path / "with space"}[scenario]
    path = w.local_config_path(project)
    assert path.parent == tmp_path / "cfg" / "sandbox-ai" / "opencode"
    assert path.name.startswith({"happy": "app-", "sad": "root-", "edge": "with space-"}[scenario])
    assert w.local_config_path(tmp_path / "other" / project.name) != path or scenario == "sad"


def test_forget(scenario, tmp_path):
    path = tmp_path / ".sandbox-ai.json"
    path.write_text("{}")
    if scenario == "sad":
        assert w.cli(["forget", str(path)]) == 1
        return
    w.launcher().trust_config(path)
    if scenario == "edge":
        path.unlink()  # forgetting works for a file that is already gone
    assert w.cli(["forget", str(path)]) == 0
    assert str(path) not in w.launcher().trusted_configs()


def test_complete_path(scenario, tmp_path, monkeypatch):
    (tmp_path / "project").mkdir()
    (tmp_path / "progress.txt").write_text("")
    (tmp_path / "odd [name]").mkdir()

    def all_matches(text):
        result, state = [], 0
        while (match := w.complete_path(text, state)) is not None:
            result.append(match)
            state += 1
        return result
    if scenario == "happy":
        # Directories end in "/", files do not.
        assert all_matches(f"{tmp_path}/pro") == [f"{tmp_path}/progress.txt", f"{tmp_path}/project/"]
    elif scenario == "sad":
        assert all_matches(f"{tmp_path}/nothing") == []
    else:
        # "~" stays as typed, and glob characters in names are taken literally.
        monkeypatch.setenv("HOME", str(tmp_path))
        assert all_matches("~/proj") == ["~/project/"]
        assert all_matches("~/odd [") == ["~/odd [name]/"]


def test_line_editing_in_a_real_terminal(scenario):
    """Backspace as ^H or DEL, cursor keys and Ctrl-U edit the line instead of ending up in the answer."""
    import pty
    import select
    import sys
    import time
    keys, expected = {"happy": (b"das\x08\x08ir\r", "dir"),
                      "sad": (b"wrong\x15right\r", "right"),
                      "edge": (b"dir\x7f\x7fx\x1b[DY\r", "dYx")}[scenario]
    master, slave = pty.openpty()
    code = (f"import sys; sys.path.insert(0, {str(ROOT / 'sandbox')!r}); import wizard; "
            "print('editing', wizard.enable_line_editing()); print('answer', repr(input('Q: ')))")
    proc = subprocess.Popen([sys.executable, "-I", "-c", code], stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    output = b""

    def read_until(marker, timeout=10):
        nonlocal output
        deadline = time.monotonic() + timeout
        while marker not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    output += os.read(master, 4096)
                except OSError:
                    break
    try:
        read_until(b"Q: ")
        if b"editing False" in output:
            pytest.skip("no readline module")
        os.write(master, keys)
        read_until(b"answer")
        read_until(b"\n", timeout=2)
        proc.wait(10)
    finally:
        os.close(master)
        if proc.poll() is None:
            proc.kill()
    assert f"answer {expected!r}".encode() in output, output


def test_line_editing_off_without_terminal(monkeypatch):
    monkeypatch.setattr(w.sys.stdin, "isatty", lambda: False)
    assert w.enable_line_editing() is False


def test_run_writes_typed_token_privately_then_launcher_plan(tmp_path, capsys):
    project = tmp_path / "proj"
    git_config(project, '[remote "origin"]\n\turl = http://localhost:3000/alice/sandbox-ai.git\n')
    out = []
    path = w.run(prompt=scripted([str(project), "", "claude", "podman", "development", "n", "n", "", "n",
                                  "n", "y", "n", "none", "y", "enter", "", "n", "y"]),
                 out=out.append, fetch=fake_fetch({}), secret=scripted([GITEA]))
    text = path.read_text()
    token_file = w.token_path("http://localhost:3000")
    # The token is in a private file, not in the project configuration, and the check did not complain.
    assert GITEA not in text and GITEA not in "\n".join(out)
    assert token_file.read_text() == GITEA + "\n" and token_file.stat().st_mode & 0o777 == 0o600
    assert not any("would refuse" in line for line in out)
    options = json.loads(text)["options"]
    assert options[:2] == ["--profile", "development"]
    assert ["--git-token-file", f"http://localhost:3000={token_file}"] == options[-2:]
    assert w.launcher().launch(["claude", "--workspace", str(project), "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["git_credentials"] == [{"url": "http://localhost:3000", "user": "x-access-token",
                                        "file": str(token_file)}]
    assert plan["host_ports"] == [3000] and "pypi.org" in plan["https_hosts"] and "api.openai.com" in plan["https_hosts"]


def test_run_cancel_writes_no_token(tmp_path):
    project = tmp_path / "proj"
    path = w.run(prompt=scripted([str(project), "", "claude", "podman", "default", "n", "n", "", "n",
                                  "n", "y", "n", "enter", "", "n", "n"]),
                 out=lambda line: None, fetch=fake_fetch({}), secret=scripted([GH]))
    assert path is None and not w.token_store().exists()


def test_review_github_origin_after_github_token(tmp_path, monkeypatch):
    """Review finding: a github.com token and the github.com origin gave two tokens for one URL."""
    project = tmp_path / "proj"
    git_config(project, '[remote "origin"]\n\turl = https://github.com/owner/repo.git\n')
    prompts = Prompts(["", "enter", "https://github.com", ""], secrets=[GH])
    options = w.ask_git(prompts.p, {"backend": "podman", "network": "default"}, project, [])
    assert options.count("--git-token-file") == 1
    assert any("has a token already" in line for line in prompts.lines)
    # The origin is not offered again: the launcher accepts the result.
    assert not any("origin" in line for line in prompts.lines)


# --- editing an existing configuration -----------------------------------------------------------


def write_trusted(project, document):
    project.mkdir(parents=True, exist_ok=True)
    path = project / ".sandbox-ai.json"
    path.write_text(json.dumps(document, indent=2) + "\n")
    w.launcher().trust_config(path)
    return path


def test_option_groups(scenario):
    if scenario == "happy":
        assert w.option_groups(["--gui", "--mount", "/a:/mnt/a:ro", "--allow-github"]) == [
            ("--gui", None, ["--gui"]), ("--mount", "/a:/mnt/a:ro", ["--mount", "/a:/mnt/a:ro"]),
            ("--allow-github", None, ["--allow-github"])]
    elif scenario == "sad":
        # Unknown tokens stay as they are; a value option at the end has no value.
        assert w.option_groups(["--nonsense", "x", "--env"]) == [
            ("--nonsense", None, ["--nonsense"]), ("x", None, ["x"]), ("--env", None, ["--env"])]
    else:
        assert w.option_groups(["--log-dir=/l", "--gui=1"]) == [("--log-dir", "/l", ["--log-dir=/l"]),
                                                                 ("--gui=1", None, ["--gui=1"])]


def test_answers_from_document(scenario, tmp_path):
    if scenario == "happy":
        document = {"version": 1, "agent": "claude", "workspaces": ["/w"], "description": "mine",
                    "agent_args": ["--model", "opus"],
                    "options": ["--backend", "bwrap", "--profile", "development", "--dangerously-skip-permissions",
                                "--no-tmux", "--log-dir", "/l", "--allow-pypi", "--allow-host", "x.org",
                                "--git-identity", "--git-token-env", "https://github.com=GH", "--mcp", "demo",
                                "--gui-vnc", "/v", "--copy", "/a:.b"]}
        answers, notes = w.answers_from_document(document)
        assert notes == []
        assert (answers["backend"], answers["network"], answers["danger"], answers["tmux"], answers["gui"],
                answers["log_dir"]) == ("bwrap", "development", True, False, True, "/l")
        assert answers["network_extra"] == ["--allow-pypi", "--allow-host", "x.org"]
        assert answers["git"] == ["--git-identity", "--git-token-env", "https://github.com=GH"]
        assert answers["extra"] == ["--mcp", "demo", "--gui-vnc", "/v", "--copy", "/a:.b"]
        # Building it again keeps every option (in the wizard's order) and the other keys.
        rebuilt = w.build_document(answers)
        assert sorted(rebuilt["options"]) == sorted(document["options"]) and rebuilt["description"] == "mine"
        assert rebuilt["agent_args"] == ["--model", "opus"]
    elif scenario == "sad":
        answers, notes = w.answers_from_document({"version": 1, "agent": "codex",
                                                  "options": ["--session", "x", "--workspace", "/y"]})
        assert answers["extra"] == [] and len(notes) == 2 and "not allowed" in notes[0]
    else:
        # A local LLM pair is recognized; profiles next to it stay as further options (review finding: they
        # were dropped), and an explicit --tmux stays explicit.
        document = {"version": 1, "agent": "opencode", "options": [
            "--local-config", "/c.json", "--allow-tcp", "10.0.0.2:11434", "--profile", "dns", "--profile", "frontier",
            "--tmux"]}
        answers, _ = w.answers_from_document(document)
        assert answers["local"] == {"config": "/c.json", "target": "10.0.0.2:11434"}
        assert answers["network"] == "default" and answers["extra"] == ["--profile", "dns", "--profile", "frontier"]
        assert answers["tmux"] and answers["tmux_forced"]
        rebuilt = w.build_document(answers)
        assert rebuilt["options"][:4] == ["--local-config", "/c.json", "--allow-tcp", "10.0.0.2:11434"]
        assert w.changes(document, rebuilt) == []


def test_edit_changes_parts_and_shows_the_difference(tmp_path, monkeypatch):
    project = tmp_path / "proj"
    monkeypatch.setenv("SANDBOX_GH", "x")
    path = write_trusted(project, {"version": 1, "agent": "claude", "description": "Created by sandbox-wizard",
                                   "options": ["--profile", "frontier", "--allow-pypi", "--git-identity",
                                               "--git-token-env", "https://github.com=SANDBOX_GH", "--copy", "/a:.b"],
                                   "workspaces": []})
    out = []
    answers = ["4", "development",                       # network
               "5", "y", "n", "n", "y",                  # behaviour: no prompts, no display, no tmux, logging
               "6", "-", "", "", "", "",                 # network extras: drop pypi, keep the rest empty
               "7", "", "y", "",                         # git: identity, keep the GitHub token, no own server
               "s", "y"]
    assert w.run(prompt=scripted(answers), out=out.append, project=project, mode="edit") == path
    document = json.loads(path.read_text())
    assert document["options"] == ["--profile", "development", "--dangerously-skip-permissions", "--no-tmux",
                                   "--log-dir", str(w.launcher().default_log_dir(project)), "--git-identity",
                                   "--git-token-env", "https://github.com=SANDBOX_GH", "--copy", "/a:.b"]
    text = "\n".join(out)
    assert "  - --profile frontier" in text and "  + --profile development" in text and "  - --allow-pypi" in text
    # Written and trusted: the launcher accepts it as it is.
    args = w.launcher().parse_args(["claude", "--workspace", str(project)])
    assert w.launcher().load_project_config(args)[1] == document


def test_edit_quit_or_untrusted_writes_nothing(scenario, tmp_path):
    project = tmp_path / "proj"
    path = write_trusted(project, {"version": 1, "agent": "codex", "options": ["--profile", "dns"]})
    before = path.read_text()
    out = []
    if scenario == "happy":
        assert w.run(prompt=scripted(["2", "bwrap", "q"]), out=out.append, project=project, mode="edit") is None
    elif scenario == "sad":
        # The agent changed the file after it was trusted: shown in full, used only after a yes.
        path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--profile", "egress-all"]}))
        before = path.read_text()
        assert w.run(prompt=scripted(["n"]), out=out.append, project=project, mode="edit") is None
        assert any("changed since it was trusted" in line for line in out)
        assert any("egress-all" in line for line in out)
    else:
        path.write_text('{"version": 1, "agent": "codex", "options": "--dns"}')
        before = path.read_text()
        assert w.run(prompt=scripted([]), out=out.append, project=project, mode="edit") is None
        assert any("options must be a list of strings" in line for line in out)
    assert path.read_text() == before


def test_edit_untrusted_after_yes_trusts_reviewed_content(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    path = project / ".sandbox-ai.json"
    path.write_text(json.dumps({"version": 1, "agent": "codex", "options": ["--profile", "dns"]}))
    out = []
    assert w.run(prompt=scripted(["y", "s", "y"]), out=out.append, project=project, mode="edit") == path
    assert any("No changes." in line for line in out)
    args = w.launcher().parse_args(["codex", "--workspace", str(project)])
    assert w.launcher().load_project_config(args)[1]["options"] == ["--profile", "dns"]


def test_edit_expert_line_and_agent_arguments(scenario, tmp_path):
    project = tmp_path / "proj"
    path = write_trusted(project, {"version": 1, "agent": "claude", "options": ["--gui", "--allow-github"],
                                   "agent_args": ["--model", "opus"]})
    if scenario == "happy":
        answers = ["0", "--allow-github --mount /x:/mnt/x:ro", "9", "-", "s", "y"]
    elif scenario == "sad":
        # Forbidden, unknown and relative values are refused and asked again.
        answers = ["0", "--session x", "0", "--nonsense", "--log-dir rel", "", "s", "y"]
    else:
        answers = ["9", "--model sonnet --effort high", "s", "y"]
    assert w.run(prompt=scripted(answers), out=lambda line: None, project=project, mode="edit") == path
    document = json.loads(path.read_text())
    if scenario == "happy":
        assert document["options"] == ["--allow-github", "--mount", "/x:/mnt/x:ro", "--no-tmux"] or \
            document["options"] == ["--allow-github", "--mount", "/x:/mnt/x:ro"]
        assert "agent_args" not in document
    elif scenario == "sad":
        assert document["options"] == ["--gui", "--allow-github"]
    else:
        assert document["agent_args"] == ["--model", "sonnet", "--effort", "high"]


def test_new_offers_edit_for_an_existing_configuration(tmp_path):
    project = tmp_path / "proj"
    path = write_trusted(project, {"version": 1, "agent": "codex", "options": []})
    out = []
    assert w.run(prompt=scripted([str(project), "", "1", "claude", "s", "y"]), out=out.append) == path
    assert json.loads(path.read_text())["agent"] == "claude"
    assert any("The options are checked for the new agent" in line for line in out)


def test_cli_edit(scenario, tmp_path, monkeypatch):
    project = tmp_path / "proj"
    calls = []
    monkeypatch.setattr(w, "run", lambda **kwargs: calls.append(kwargs) or project / ".sandbox-ai.json")
    monkeypatch.setattr(w, "enable_line_editing", lambda: False)
    if scenario == "happy":
        assert w.cli(["edit", str(project)]) == 0
    elif scenario == "sad":
        monkeypatch.setattr(w, "run", lambda **kwargs: None)
        assert w.cli(["edit", str(project)]) == 1
        return
    else:
        assert w.cli(["edit", str(project / ".sandbox-ai.json")]) == 0
    assert calls == [{"project": project, "mode": "edit"}]


def test_prompter_edit(scenario):
    if scenario == "happy":
        # In a terminal the current text is put on the line for editing.
        seen = []
        p = w.Prompter(scripted([]), print, prefill=lambda prompt, text: seen.append(text) or text + " --gui")
        assert p.edit("Options", "--dns") == "--dns --gui" and seen == ["--dns"]
    elif scenario == "sad":
        p = w.Prompter(scripted(["-"]), print)
        assert p.edit("Options", "--dns") == ""
    else:
        p = w.Prompter(scripted(["", ""]), print)
        assert p.edit("Options", "--dns") == "--dns" and p.edit("Options", "") == ""
