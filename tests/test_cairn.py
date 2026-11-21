"""Cairn in sandbox-ai (--cairn: sandbox/cairn.py, cairn_runtime.py, cairn_relay.py, docs/CAIRN_REVIEW.md): a part of
its own beside the agent - its own container in the session's pod, on the agent's network, its board opened in the
sandbox's browser - whose workers are the session's agents through a narrow relay, never a Docker or Podman socket;
the image at the reviewed commit, installed unchanged with its dependencies hash-locked; the public presets
cairn-pentest-board and cairn-local-llm.

The user: "Cairn als eigener Teil im Board, wie ein Service-Container neben dem Agent; sein Board öffnet sich
automatisch im Browser-Tab, die Agents der Session sind seine Worker." - and "Cairn natürlich auch mit anderen
Modellen / lokalem Preset supporten." """
import argparse
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("launcher_for_cairn_tests", "sandbox/agent_sandbox.py")
cairn = load("cairn_tests", "sandbox/cairn.py")
browser = load("browser_for_cairn_tests", "sandbox/browser.py")
services = load("services_for_cairn_tests", "sandbox/services.py")
images = load("images_cairn_tests", "sandbox/images.py")
presets = load("presets_cairn_tests", "sandbox/presets.py")
modes = load("modes_cairn_tests", "sandbox/modes.py")
runtime = load("runtime_cairn_tests", "sandbox/runtime.py")
cairn._launcher = browser._launcher = services._launcher = s
services._browser, services._cairn = browser, cairn
CONTAINERFILE = ROOT / "sandbox/Containerfile.cairn"
LOCK = ROOT / "sandbox/Containerfile.cairn.lock"
PRESET = json.loads((ROOT / "sandbox/presets/cairn-pentest-board.json").read_text())
LOCAL = json.loads((ROOT / "sandbox/presets/cairn-local-llm.json").read_text())
IMAGE_ID = "c" * 64


@pytest.fixture(autouse=True)
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    (tmp_path / "runtime").mkdir()
    (tmp_path / "state" / "sandbox-ai").mkdir(parents=True)
    (tmp_path / "state" / "sandbox-ai" / "exposed.json").write_text("[]")
    monkeypatch.setattr(s, "load_cairn_module", lambda: cairn)
    monkeypatch.setattr(s, "load_browser_module", lambda: browser)
    monkeypatch.setattr(s, "load_services_module", lambda: services)
    monkeypatch.setattr(s, "on_machine", lambda a: False)
    return tmp_path


def start(*options, agent="claude", keep=False):
    a = s.parse_args([agent, *options])
    a.vpn_profile = None
    a.run_id = f"{agent}-1234abcd"
    a.keep_home = keep
    a.services = []
    return a


def checked(*options, **kwargs):
    a = start(*options, **kwargs)
    s.check_cairn(a)
    s.check_browser(a)
    return a


# -- the image --

def test_the_containerfile_installs_the_reviewed_commit_unchanged_and_no_agent():
    text = CONTAINERFILE.read_text()
    assert "ARG DEBIAN_IMAGE=docker.io/library/debian:bookworm-slim" in text and "team-sandbox" not in text
    assert f"ARG CAIRN_COMMIT={cairn.COMMIT}" in text and 'test "$(git -C /tmp/cairn-src rev-parse HEAD)" = "${CAIRN_COMMIT}"' in text
    assert "https://github.com/oritera/Cairn.git" in text and cairn.REPOSITORY.endswith("oritera/Cairn")
    assert "--require-hashes --no-deps" in text and "--index-url https://pypi.org/simple" in text
    assert "COPY Containerfile.cairn.lock" in text and "dispatch.yaml" not in text
    assert "uv python install --no-bin 3.12" in text and re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert "cp /tmp/cairn-src/LICENSE /tmp/cairn-src/README.md /opt/cairn/" in text
    assert not re.search(r"\b(sed|patch)\b[^\n]*/opt/cairn/src", text), "nothing of Cairn's is edited"
    assert "docker.sock" not in text
    final = text.rsplit("\nFROM ", 1)[1]
    assert final.startswith("${DEBIAN_IMAGE}") and "COPY --from=build /opt/cairn /opt/cairn" in final
    assert not re.search(r"\b(npm|claude|codex|apt-get install)\b", final), "nothing of an agent in what runs"


def test_the_lock_is_upstreams_hashed_for_linux_only():
    text = LOCK.read_text()
    requirements = re.findall(r"^([a-z0-9-]+)==([^ ;\\]+)", text, re.M)
    names = {name for name, _ in requirements}
    assert {"fastapi", "uvicorn", "click", "pyyaml", "requests", "docker"} <= names
    assert not {"pywin32", "colorama", "pytest", "httpx"} & names, "no Windows-only or dev packages"
    blocks = re.split(r"\n(?=[a-z])", text.split("\n", 4)[4])
    assert all(re.search(r"--hash=sha256:[0-9a-f]{64}", block) for block in blocks), "every requirement hashed"
    assert dict(requirements)["fastapi"] == "0.135.3" and dict(requirements)["uvicorn"] == "0.44.0"
    assert "scripts/cairn_lock.py" in text


def test_the_lock_script_filters_like_hermes_lock():
    script = load("cairn_lock_tests", "scripts/cairn_lock.py")
    module = script.hermes_lock()
    exported = ("fastapi==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
                "pywin32==311 ; sys_platform == 'win32' \\\n    --hash=sha256:" + "b" * 64 + "\n"
                "uvloop==0.22.1 ; platform_python_implementation != 'PyPy' and sys_platform != 'win32' \\\n"
                "    --hash=sha256:" + "c" * 64 + "\n")
    kept = module.keep(exported)
    assert kept.startswith(script.HEADER) and "fastapi==1.0" in kept and "uvloop" in kept and "pywin32" not in kept
    with pytest.raises(ValueError, match="no hash"):
        module.keep("requests==2.0\n")
    assert script.OUT == LOCK


def test_the_build_target_and_the_lists_know_cairn():
    assert "cairn" in images.BUILD_TARGETS and "cairn" not in images.AGENTS
    assert '"cairn"' in (ROOT / "sandbox/web/app.js").read_text()
    assert "cairn" in (ROOT / "sandbox/webui.py").read_text()
    assert s.CAIRN_IMAGE == cairn.IMAGE == "localhost/sandbox-ai-cairn:latest"
    assert s.image_fix(start(), "claude", cairn.IMAGE) == "sandbox-ai build cairn"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A podman that records its calls; a pulled Debian base has a digest."""
    log = tmp_path / "podman.log"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) "
                      "+ '\\n')\nwords = [w for w in sys.argv[1:] if not w.startswith('--cgroup')]\n"
                      "if words[:2] == ['image', 'inspect']:\n    print('sha256:' + 'd' * 64)\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    return lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def build(*args):
    return subprocess.run(["/bin/bash", str(ROOT / "sandbox/build_image.sh"), *args], capture_output=True, text=True)


def test_build_cairn_needs_no_agent_image_pins_its_base_and_takes_the_reviewed_commit_only(engine):
    result = build("cairn")
    assert result.returncode == 0, result.stderr
    built = [call for call in engine() if "build" in call]
    assert built and "localhost/sandbox-ai-cairn:latest" in built[0] and f"sandbox-ai.cairn-commit={cairn.COMMIT}" in built[0]
    assert "sandbox-ai.role=cairn" in built[0] and any(arg.endswith("Containerfile.cairn") for arg in built[0])
    assert f"DEBIAN_IMAGE=docker.io/library/debian@sha256:{'d' * 64}" in built[0]
    assert not any("team-sandbox" in word for call in engine() for word in call)
    other = build("cairn", "--build-arg", "CAIRN_COMMIT=e0ef2f850e5805f824815ee38f049e066deeb7d1")
    assert other.returncode == 2 and "commit sandbox-ai reviewed" in other.stderr
    assert build("--pin", "1.0.0", "cairn").returncode == 2
    assert "cairn" in build("--help").stdout


# -- the start's options --

def test_the_options_and_their_short_forms():
    a = s.parse_args(["claude", "-cr", "-cw", "claude=sonnet", "-co", "10.0.0.5", "-cg", "its services", "-ch", "port 80",
                      "-cb", "session"])
    assert (a.cairn, a.cairn_workers_text, a.cairn_origin, a.cairn_goal, a.cairn_hint, a.cairn_board) == \
        (True, "claude=sonnet", "10.0.0.5", "its services", "port 80", "session")
    plain = s.parse_args(["claude"])
    assert plain.cairn is False and plain.cairn_workers_text is None
    with pytest.raises(SystemExit):
        s.parse_args(["claude", "--cairn-board", "elsewhere"])


def test_the_session_s_agents_are_cairn_s_workers_and_its_board_opens_in_the_browser():
    """"die Agents der Session sind seine Worker" and "sein Board öffnet sich automatisch im Browser-Tab"."""
    a = checked("--cairn")
    assert [w["agent"] for w in a.cairn_workers] == ["claude"]
    assert a.cairn_container["name"] == "cairn" and a.cairn_container["ports"] == [8000]
    assert a.browser == "chromium" and a.cairn_browser and a.browser_container is not None
    assert browser.config(a)["start_url"] == "http://127.0.0.1:8000/"
    assert [m["name"] for m in services.members(a)] == ["cairn", "browser"], "the board before the browser opens it"
    team = checked("--cairn", "--with", "codex")
    assert [w["agent"] for w in team.cairn_workers] == ["claude", "codex"]
    assert [w["agent"] for w in checked("--cairn", agent="codex").cairn_workers] == ["codex"]
    own = checked("--cairn", "--browser", "firefox")
    assert own.browser == "firefox" and not own.cairn_browser and browser.config(own)["start_url"].endswith(":8000/")
    assert "start_url" not in browser.config(checked("--browser"))


def test_on_macos_no_browser_comes_along(monkeypatch):
    monkeypatch.setattr(s, "on_machine", lambda a: True)
    a = start("--cairn")
    s.check_cairn(a)
    assert a.browser is None and not a.cairn_browser
    assert any("no browser shows the board" in note for note in cairn.notes(a))


def test_workers_with_their_own_models():
    """"Cairn natürlich auch mit anderen Modellen": a model per worker, else what the session gave the agent."""
    a = checked("--cairn", "--with", "codex", "--cairn-workers", "codex=gpt-5.2-codex,claude=opus", "--model", "sonnet")
    assert a.cairn_workers == [{"agent": "codex", "model": "gpt-5.2-codex"}, {"agent": "claude", "model": "opus"}]
    assert cairn.relay_config(a, s.launcher_namespace())["workers"] == {"codex": ["--model", "gpt-5.2-codex"],
                                                                         "claude": ["--model", "opus"]}
    lead = checked("--cairn", "--with", "codex", "--model", "sonnet", "--effort", "high")
    assert cairn.relay_config(lead, s.launcher_namespace())["workers"] == {
        "claude": ["--model", "sonnet", "--effort", "high"], "codex": []}, "an agent --with keeps its own default"
    dispatch = cairn.dispatch(a)
    assert [w["type"] for w in dispatch["workers"]] == ["codex", "claudecode"]
    assert dispatch["workers"][0]["task_types"] == ["bootstrap", "reason", "explore"]
    assert dispatch["workers"][1]["task_types"] == ["explore"]


def test_codex_on_an_llm_server_keeps_its_profile(monkeypatch):
    a = checked("--cairn", agent="codex")
    a.llm = "ollama"
    monkeypatch.setattr(s, "load_llm_module", lambda: argparse.Namespace(agent_arguments=lambda a: ["--profile",
                                                                                                   "sandbox-ai-llm"]))
    assert cairn.relay_config(a, s.launcher_namespace())["workers"] == {"codex": ["--profile", "sandbox-ai-llm"]}


@pytest.mark.parametrize("options, agent, said", [
    (("--cairn-goal", "x"), "claude", "add --cairn"),
    (("--cairn", "--cairn-workers", "pi"), "claude", "pi driver"),
    (("--cairn", "--cairn-workers", "codex"), "claude", "add --with codex"),
    (("--cairn", "--cairn-workers", "claude,claude"), "claude", "twice"),
    (("--cairn", "--cairn-workers", "claude=a b"), "claude", "not a model name"),
    (("--cairn",), "hermes", "none of Cairn's"),
    (("--cairn", "--cairn-origin", "10.0.0.5"), "claude", "go together"),
    (("--cairn", "--cairn-hint", "port 80"), "claude", "first hint"),
    (("--cairn", "--cairn-board", "session", "--no-keep"), "claude", "keeps none"),
    (("--cairn", "--cairn-goal", "x", "--cairn-origin", "a\x07b"), "claude", "control characters"),
    (("--cairn", "--cairn-goal", "x" * 4001, "--cairn-origin", "y"), "claude", "at most"),
    (("--cairn", "--allow-host-port", "8000"), "claude", "port of the host"),
    (("--cairn", "--backend", "bwrap"), "claude", "Podman backend"),
])
def test_what_the_launcher_refuses(options, agent, said):
    a = start(*options, agent=agent)
    with pytest.raises(ValueError, match=re.escape(said)):
        s.check_cairn(a)


def test_a_model_of_its_own_is_refused_where_llm_gives_it():
    a = start("--cairn", "--cairn-workers", "claude=opus")
    a.llm = "ollama"
    with pytest.raises(ValueError, match="with --llm the model is the server's"):
        s.check_cairn(a)


def test_a_service_cannot_take_its_name_or_port():
    a = start("--cairn")
    a.services = [{"name": "cairn", "ports": []}]
    with pytest.raises(ValueError, match="name it otherwise"):
        s.check_cairn(a)
    a.services = [{"name": "web", "ports": [8000]}]
    with pytest.raises(ValueError, match="port 8000"):
        s.check_cairn(a)


def test_the_project_of_the_start():
    a = checked("--cairn", "--cairn-origin", "The machine 10.0.0.5\nonly port 80", "--cairn-goal", "Its flaws",
                "--cairn-hint", "Start at port 80")
    assert a.cairn_project == {"title": "sandbox-ai: The machine 10.0.0.5", "origin": "The machine 10.0.0.5\nonly port 80",
                               "goal": "Its flaws", "hints": [{"content": "Start at port 80", "creator": "human"}]}
    assert cairn.config(a)["project"] == a.cairn_project and checked("--cairn").cairn_project is None


# -- the containers --

def test_cairn_s_container_has_no_rights_beyond_the_pod_and_only_the_relay_s_folder(config):
    a = checked("--cairn")
    a.control_dir = config / "runs" / "claude-1234abcd"
    service = a.cairn_container
    service["record"]["id"] = IMAGE_ID
    argv, env = cairn.create_command(a, "agent-claude-1234abcd", service, config / "session", s.launcher_namespace(),
                                     limits=set())
    assert env == {}
    assert argv[argv.index("--pod") + 1] == "agent-claude-1234abcd" and "--cap-drop=ALL" in argv
    assert "--security-opt=no-new-privileges" in argv and "--read-only" in argv and argv[argv.index("--user") + 1] == "0:0"
    assert "--pull=never" in argv and argv[-3:] == [IMAGE_ID, "-I", "/sandbox-cairn/cairn_runtime.py"]
    assert argv[argv.index("--entrypoint") + 1] == "/opt/cairn/venv/bin/python"
    mounts = [argv[i + 1] for i, word in enumerate(argv) if word == "--mount"]
    relay = next(m for m in mounts if "dst=/sandbox-cairn/relay" in m)
    assert f"src={config / 'runs' / 'claude-1234abcd.cairn'}" in relay and ",ro," in relay
    assert not any("/work" in m.split("dst=")[1] for m in mounts if "dst=" in m), "no file of the project"
    assert not any(re.search(r"docker\.sock|podman\.sock|/run/user", word) for word in argv), "no engine socket"
    assert "/data:rw,noexec,nosuid,nodev,size=256m" in argv, "a fresh board: a tmpfs"
    assert not any(word.startswith("--network") or word == "--privileged" for word in argv)


def test_a_board_kept_with_the_session(config, monkeypatch):
    a = checked("--cairn", "--cairn-board", "session", keep=True)
    a.start_key = "brisk-otter-lantern"
    a.control_dir = config / "runs" / "claude-1234abcd"
    a.cairn_container["record"]["id"] = IMAGE_ID
    monkeypatch.setattr(s, "load_sessions_module", lambda: argparse.Namespace(session_dir=lambda key: config / "s" / key))
    argv, _ = cairn.create_command(a, "pod", a.cairn_container, config / "session", s.launcher_namespace(), limits=set())
    assert any(f"src={config / 's' / 'brisk-otter-lantern' / 'cairn'},dst=/data,rw" in w for w in argv)
    assert not any(w.startswith("/data:") for w in argv)


def test_the_agent_s_container_gets_the_relay_and_its_folder(config, monkeypatch):
    a = checked("--cairn")
    a.control_dir = config / "runs" / "claude-1234abcd"
    mounts = cairn.agent_mounts(a, s.launcher_namespace())
    assert any(m.endswith("dst=/sandbox/cairn_relay.py,ro,nodev,nosuid") and "cairn_relay.py" in m for m in mounts)
    assert any(f"src={config / 'runs' / 'claude-1234abcd.cairn'},dst=/sandbox/cairn,rw" in m for m in mounts)
    assert cairn.agent_mounts(start(), s.launcher_namespace()) == []
    monkeypatch.setattr(s, "on_machine", lambda a: True)  # macOS: a socket does not cross the VM's shared folders
    assert "type=volume,src=sandbox-ai-cairn-claude-1234abcd,dst=/sandbox/cairn" in \
        cairn.agent_mounts(a, s.launcher_namespace())
    assert "type=volume,src=sandbox-ai-cairn-claude-1234abcd,dst=/sandbox-cairn/relay,ro" in \
        cairn.relay_mount(a, s.launcher_namespace())("/sandbox-cairn/relay", "ro")


def test_the_services_launch_makes_cairn_s_container_with_cairn_s_line(config, monkeypatch):
    a = checked("--cairn")
    made = []
    monkeypatch.setattr(cairn, "create_command", lambda *args: made.append("cairn") or (["podman", "create"], {}))
    monkeypatch.setattr(browser, "create_command", lambda *args: made.append("browser") or (["podman", "create"], {}))
    assert services.cairn_module() is cairn
    for member in services.members(a):
        (browser.create_command if member.get("browser") else
         services.cairn_module().create_command if member.get("cairn") else services.create_command)(a, "p", member, "s")
    assert made == ["cairn", "browser"]
    assert [entry["name"] for entry in services.supervisor_config(a)] == ["cairn", "browser"]
    assert services.supervisor_config(a)[0]["ready"] == 8000


def test_the_relay_is_started_inside_the_guard_with_the_agent_s_environment():
    started = []
    runtime.start_cairn_relay({"socket": "/sandbox/cairn/relay.sock", "workers": {"claude": []}}, {"HOME": "/root"},
                              popen=lambda argv, **kw: started.append((argv, kw)))
    [(argv, kw)] = started
    assert argv[:4] == ["/usr/bin/python3", "-I", "-S", "/sandbox/cairn_relay.py"] and argv[4] == "supervise"
    assert json.loads(argv[5])["socket"] == "/sandbox/cairn/relay.sock" and kw["env"] == {"HOME": "/root"}
    assert kw["start_new_session"] and kw["stdout"] == subprocess.DEVNULL


def test_the_dispatcher_runs_only_locally_with_the_session_s_agents():
    a = checked("--cairn", "--with", "codex")
    dispatch = cairn.dispatch(a)
    assert dispatch["runtime"]["execution"] == "local" and dispatch["server"] == "http://127.0.0.1:8000"
    assert "docker" not in json.dumps(dispatch).lower() and "common_env" not in dispatch
    assert dispatch["local"]["workspace_root"] == "/work/cairn/runs"
    assert dispatch["runtime"]["max_workers"] == 2 and dispatch["runtime"]["max_running_projects"] == 1
    assert set(cairn.DRIVERS) == {"claude", "codex"}, "not pi: its local driver runs a shell line"


def test_the_dry_run_plan_and_run_json(config):
    a = checked("--cairn", "--with", "codex", "--model", "sonnet")
    plan = cairn.plan(a, s.launcher_namespace())
    assert plan["image"] == cairn.IMAGE and plan["board"] == "http://127.0.0.1:8000" and plan["commit"] == cairn.COMMIT
    assert plan["workers"][0] == {"agent": "claude", "driver": "claudecode", "model": "the agent's own",
                                  "arguments": ["--model", "sonnet"]}
    assert "never a shell line" in plan["relay"]["takes"] and plan["browser"] == "starts on the board"
    assert cairn.record(a, "agent-x") == {"board": "http://127.0.0.1:8000", "container": "agent-x-service-cairn",
                                         "workers": ["claude", "codex"]}
    assert cairn.agent_environment(a) == {"SANDBOX_AI_CAIRN": "http://127.0.0.1:8000"}
    assert cairn.plan(start(), s.launcher_namespace()) is None


def test_the_image_must_be_sandbox_ai_s_cairn(config):
    a = checked("--cairn")
    answers = {"out": f"sha256:{IMAGE_ID} cairn"}
    run = lambda *args, **kw: subprocess.CompletedProcess(args, 0, answers["out"], "")  # noqa: E731
    assert cairn.prepare(a, s.launcher_namespace(), run) == IMAGE_ID and a.cairn_container["record"]["id"] == IMAGE_ID
    answers["out"] = f"sha256:{IMAGE_ID} browser"
    with pytest.raises(ValueError, match="sandbox-ai build cairn"):
        cairn.prepare(a, s.launcher_namespace(), run)


def test_the_security_modes():
    """paranoid: nothing acts without asking, and Cairn starts the session's agents without their questions."""
    assert "--cairn" in modes.WEAKENING_OPTIONS and "--cairn-workers" in modes.WEAKENING_OPTIONS
    assert [names for names, rule, _ in modes.RULES if rule(start("--cairn"), None) == "--cairn"] == [("paranoid",)]


# -- the presets --

def test_the_pentest_preset_uses_cairn_s_own_part():
    assert cairn.PRESET in presets.catalog()
    assert PRESET["id"] == cairn.PRESET and PRESET["category"] == "security" and PRESET["agents"] == ["claude"]
    options = PRESET["options"]
    assert "--cairn" in options and "--image" not in options and options[options.index("--with") + 1] == "codex"
    assert "{target}:{port}" in options and "--browser" not in options, "--cairn brings the browser"
    assert options[options.index("--cairn-goal") + 1] == "{goal}" and options[options.index("--cairn-hint") + 1] == "{hint}"
    assert "{target}" in options[options.index("--cairn-origin") + 1]
    values = PRESET["values"]
    assert values["target"]["kind"] == "host" and not values["target"].get("optional")
    assert values["vpn"]["optional"] and values["domain"]["optional"] and values["port2"]["optional"]
    prompt = PRESET["prompt"]
    assert "127.0.0.1:8000" in prompt and "cairn serve" not in prompt and "cairn dispatch" not in prompt
    assert '\\"status\\": \\"stopped\\"' in json.dumps(prompt) and "FINDINGS.md" in prompt
    notes = " ".join(PRESET["notes"])
    for said in ("AGPL", "unchanged", "Docker", "relay", "build cairn", "build team", "Podman only", "5 failed",
                 "cairn-local-llm"):
        assert said in notes, said
    assert PRESET["translations"]["de"]["title"] and PRESET["translations"]["de"]["description"]
    given = {"target": "10.10.10.5", "port": "80"}
    expanded = presets.expand({**PRESET, "_path": "x"}, given, extras=False)
    assert "--vpn" not in expanded and expanded.count("--allow-tcp") == 1 and "10.10.10.5:80" in expanded
    assert "The machine 10.10.10.5" in expanded[expanded.index("--cairn-origin") + 1]


def test_the_local_preset_runs_cairn_s_workers_on_your_model():
    """"Cairn natürlich auch mit anderen Modellen / lokalem Preset supporten"."""
    assert cairn.LOCAL_PRESET in presets.catalog() and LOCAL["id"] == cairn.LOCAL_PRESET
    assert LOCAL["category"] == "llm" and LOCAL["agents"] == ["claude"]
    options = LOCAL["options"]
    assert options[options.index("--llm") + 1] == "{llm}" and "--no-provider-defaults" in options
    assert options[options.index("--llm-param") + 1] == "loop_guard=on" and "--cairn" in options
    assert options[options.index("--cairn-workers") + 1] == "claude"
    assert LOCAL["values"]["llm"]["kind"] == "llm"
    assert all(LOCAL["values"][name].get("optional") for name in ("origin", "goal", "hint"))
    bare = presets.expand({**LOCAL, "_path": "x"}, {"llm": "ollama:qwen3-coder"}, extras=False)
    assert bare == ["--llm", "ollama:qwen3-coder", "--no-provider-defaults", "--llm-param", "loop_guard=on", "--cairn",
                    "--cairn-workers", "claude"]
    notes = " ".join(LOCAL["notes"])
    for said in ("loop guard", "context window", "OpenRouter", "relay", "AGPL"):
        assert said in notes, said
    assert LOCAL["translations"]["de"]["title"] == "Cairn mit einem lokalen Modell"


def test_the_review_names_the_commit_the_part_and_its_verdict():
    review = (ROOT / "docs/CAIRN_REVIEW.md").read_text()
    assert cairn.COMMIT in review and "## Go / No-Go" in review and "AGPL" in review
    for heading in ("## Hosts contacted (measured)", "## Telemetry", "## What the dispatcher starts",
                    "## The Docker socket", "## A part of its own: the design", "## The relay",
                    "## Models and providers"):
        assert heading in review, heading
    assert "CLAUDE_CODE_MAX_CONTEXT_TOKENS" in review and "cairn-local-llm" in review
    for readme in ("README.md", "README_DE.md"):
        text = (ROOT / readme).read_text()
        assert "cairn-pentest-board" in text and "--cairn" in text and "cairn-local-llm" in text, readme


def test_a_killed_launcher_s_relay_folder_goes_with_its_run(tmp_path):
    """Analysis round 3 (chaos): a launcher killed with SIGKILL left its run's folder, which `sandbox-ai stop` and gc
    remove (liveness.discard) with the browser's folders beside it - but not RUN.cairn, the relay's folder with its
    socket and its log of the workers' calls, which stayed for good."""
    liveness = load("liveness_for_cairn_tests", "sandbox/liveness.py")
    run = tmp_path / "runs" / "claude-0123abcd"
    run.mkdir(parents=True)
    relay = cairn.relay_dir(argparse.Namespace(control_dir=run, podman_url=None), s)
    assert relay == run.with_name(run.name + ".cairn")
    relay.mkdir()
    (relay / "relay.log").write_text("started claude task in /work/cairn/runs/x\n")
    other = tmp_path / "runs" / "claude-0123abcd-other.cairn"
    other.mkdir()
    liveness.discard(run)
    assert sorted(p.name for p in run.parent.iterdir()) == [other.name]
