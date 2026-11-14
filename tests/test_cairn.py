"""Cairn in sandbox-ai (sandbox/cairn.py, docs/CAIRN_REVIEW.md): the image on the team image at the reviewed commit,
installed unchanged with its dependencies hash-locked, only its local execution mode (never a Docker socket), its
server on the sandbox's loopback, the build target, and the public preset cairn-pentest-board."""
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cairn = load("cairn_tests", "sandbox/cairn.py")
images = load("images_cairn_tests", "sandbox/images.py")
presets = load("presets_cairn_tests", "sandbox/presets.py")
CONTAINERFILE = ROOT / "sandbox/Containerfile.cairn"
LOCK = ROOT / "sandbox/Containerfile.cairn.lock"
DISPATCH = ROOT / "sandbox/recipes/cairn/dispatch.yaml"
PRESET = json.loads((ROOT / "sandbox/presets/cairn-pentest-board.json").read_text())


def test_the_containerfile_installs_the_reviewed_commit_unchanged_on_the_team_image():
    text = CONTAINERFILE.read_text()
    assert "FROM localhost/team-sandbox:latest" in text
    assert f"ARG CAIRN_COMMIT={cairn.COMMIT}" in text and 'test "$(git -C /tmp/cairn-src rev-parse HEAD)" = "${CAIRN_COMMIT}"' in text
    assert "https://github.com/oritera/Cairn.git" in text and cairn.REPOSITORY.endswith("oritera/Cairn")
    assert "--require-hashes --no-deps" in text and "--index-url https://pypi.org/simple" in text
    assert "COPY Containerfile.cairn.lock" in text and "COPY recipes/cairn/dispatch.yaml /opt/cairn/dispatch.yaml" in text
    assert "uv python install --no-bin 3.12" in text and re.search(r"^ARG UV_VERSION=\d+\.\d+\.\d+$", text, re.M)
    assert not re.search(r"(curl|wget)[^\n|]*\|\s*(ba|z)?sh", text), "never a download piped into a shell"
    assert "cp /tmp/cairn-src/LICENSE /tmp/cairn-src/README.md /opt/cairn/" in text
    # Unchanged: nothing of Cairn's is edited (no sed, patch or rewrite of its files).
    assert not re.search(r"\b(sed|patch)\b[^\n]*/opt/cairn/src", text)
    assert "docker.sock" not in text


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


def test_the_dispatcher_runs_only_locally_with_the_sandbox_s_agents():
    text = DISPATCH.read_text()
    code = [line.split("#")[0] for line in text.splitlines()]
    assert re.search(r"^  execution: local\b", text, re.M) and not any("docker" in line.lower() for line in code)
    assert 'server: "http://127.0.0.1:8000"' in text and cairn.SERVER == "http://127.0.0.1:8000"
    assert re.findall(r'type: "(\w+)"', text) == ["claudecode", "codex"], "the team image's agents, not pi"
    assert "common_env" not in text.split("#")[0] and "/work/cairn/runs" in text


def test_the_build_target_and_the_lists_know_cairn():
    assert "cairn" in images.BUILD_TARGETS and "cairn" not in images.AGENTS
    assert '"cairn"' in (ROOT / "sandbox/web/app.js").read_text()
    assert "cairn" in (ROOT / "sandbox/webui.py").read_text()


@pytest.fixture
def engine(tmp_path, monkeypatch):
    """A podman that records its calls; `image exists` answers as TEAM says."""
    log = tmp_path / "podman.log"
    flag = tmp_path / "team-exists"
    script = tmp_path / "bin" / "podman"
    script.parent.mkdir()
    script.write_text(f"#!{sys.executable}\nimport json, os, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) "
                      "+ '\\n')\nwords = [w for w in sys.argv[1:] if not w.startswith('--cgroup')]\nif words[:2] == ['image', 'exists']:\n"
                      f"    sys.exit(0 if os.path.exists({str(flag)!r}) else 1)\n")
    script.chmod(0o700)
    monkeypatch.setenv("PATH", f"{script.parent}:/usr/bin:/bin")
    monkeypatch.setenv("SANDBOX_AI_PYTHON", sys.executable)
    monkeypatch.setenv("SANDBOX_AI_BUILD_NO_DNS_CHECK", "1")
    calls = lambda: [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []  # noqa: E731
    return calls, flag


def build(*args):
    return subprocess.run(["/bin/bash", str(ROOT / "sandbox/build_image.sh"), *args], capture_output=True, text=True)


def test_build_cairn_needs_the_team_image_and_takes_the_reviewed_commit_only(engine):
    calls, team = engine
    refused = build("cairn")
    assert refused.returncode == 1 and "sandbox-ai build team first" in refused.stderr
    team.write_text("")
    result = build("cairn")
    assert result.returncode == 0, result.stderr
    built = [call for call in calls() if "build" in call]
    assert built and "localhost/cairn-sandbox:latest" in built[0] and f"sandbox-ai.cairn-commit={cairn.COMMIT}" in built[0]
    assert any(arg.endswith("Containerfile.cairn") for arg in built[0])
    other = build("cairn", "--build-arg", "CAIRN_COMMIT=e0ef2f850e5805f824815ee38f049e066deeb7d1")
    assert other.returncode == 2 and "commit sandbox-ai reviewed" in other.stderr
    assert build("--pin", "1.0.0", "cairn").returncode == 2
    assert "cairn" in build("--help").stdout


def test_the_preset_is_public_scoped_and_says_what_cairn_does():
    assert cairn.PRESET in presets.catalog()
    assert PRESET["id"] == cairn.PRESET and PRESET["category"] == "security" and PRESET["agents"] == ["claude"]
    options = PRESET["options"]
    assert options[options.index("--with") + 1] == "codex" and options[options.index("--image") + 1] == cairn.IMAGE
    assert "{target}:{port}" in options and "--browser" in options and options[options.index("--vpn") + 1] == "{vpn}"
    assert options[options.index("--allow-domain") + 1] == "{domain}"
    values = PRESET["values"]
    assert values["target"]["kind"] == "host" and not values["target"].get("optional")
    assert values["vpn"]["optional"] and values["domain"]["optional"] and values["port2"]["optional"]
    assert values["hint"]["kind"] == "text" and values["goal"]["kind"] == "text"
    prompt = PRESET["prompt"]
    assert "cairn serve" in prompt and "cairn dispatch" in prompt and "{hint}" in prompt and "127.0.0.1:8000" in prompt
    notes = " ".join(PRESET["notes"])
    for said in ("AGPL", "unchanged", "Docker", "local execution", "again", "Ctrl-C", "build cairn", "Podman only"):
        assert said in notes, said
    assert PRESET["translations"]["de"]["title"] and PRESET["translations"]["de"]["description"]


def test_the_preset_expands_with_and_without_its_optional_values():
    given = {"target": "10.10.10.5", "port": "80"}
    options = presets.expand({**PRESET, "_path": str(ROOT / "sandbox/presets/x.json")}, given, extras=False)
    assert "--vpn" not in options and "--allow-domain" not in options and "10.10.10.5:80" in options
    assert options.count("--allow-tcp") == 1
    full = presets.expand({**PRESET, "_path": "x"}, {**given, "port2": "443", "domain": "lab.example.org",
                                                     "vpn": "lab"}, extras=False)
    assert full[full.index("--vpn") + 1] == "lab" and "10.10.10.5:443" in full and "lab.example.org" in full


def test_the_review_names_the_commit_and_its_verdict():
    review = (ROOT / "docs/CAIRN_REVIEW.md").read_text()
    assert cairn.COMMIT in review and "## Go / No-Go" in review and "AGPL" in review
    for heading in ("## Hosts contacted (measured)", "## Telemetry", "## What the dispatcher starts",
                    "## The Docker socket"):
        assert heading in review, heading
    for readme in ("README.md", "README_DE.md"):
        assert "cairn-pentest-board" in (ROOT / readme).read_text(), readme
