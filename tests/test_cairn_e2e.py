"""Cairn as a part of its own (--cairn), end to end: the real ./sandbox-ai entry point as a user runs it, with only
Podman, tmux and npm faked (tests/live_fakes.py). A start with Cairn gets as far as making its pod with Cairn's
container planned beside the agent, the relay's folder only the user's, the relay's configuration for the agent's
container and Cairn's own; the dry run shows the part, its workers and the board the browser opens; the presets
expand to it; what the launcher refuses is said.

The user: "Cairn als eigener Teil im Board, wie ein Service-Container neben dem Agent; sein Board öffnet sich
automatisch im Browser-Tab, die Agents der Session sind seine Worker." - and "Cairn natürlich auch mit anderen
Modellen / lokalem Preset supporten." """
import json
from pathlib import Path
import time

import pytest

import live_fakes as F

CAIRN = "localhost/sandbox-ai-cairn:latest"
BROWSER = "localhost/sandbox-ai-browser:latest"


@pytest.fixture
def host(tmp_path):
    found = F.Host(tmp_path)
    found.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest", "localhost/team-sandbox:latest")
    found.add_image(CAIRN, labels={"sandbox-ai.role": "cairn"})
    found.add_image(BROWSER, labels={"sandbox-ai.role": "browser"})
    return found


def plan(host, *args):
    done = host.run(*args, "--dry-run")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def test_the_dry_run_shows_cairn_beside_the_agent_its_workers_and_the_board_in_the_browser(host):
    """"wie ein Service-Container neben dem Agent", "sein Board öffnet sich automatisch im Browser-Tab", "die Agents
    der Session sind seine Worker"."""
    found, said = plan(host, "claude", "--with", "codex", "--cairn")
    cairn = found["cairn"]
    assert cairn["container"].endswith("-service-cairn") and cairn["image"] == CAIRN and cairn["user"] == "0:0"
    assert cairn["board"] == "http://127.0.0.1:8000" and cairn["read_only"] and "none" in cairn["capabilities"]
    assert [w["agent"] for w in cairn["workers"]] == ["claude", "codex"]
    assert cairn["dispatch"]["runtime"]["execution"] == "local" and cairn["relay"]["workspace"] == "/work/cairn/runs"
    assert found["browser"]["start_url"] == "http://127.0.0.1:8000/" and found["browser"]["control"] == "human"
    assert "Cairn's board in its own container beside the agent" in said and "never a Docker or Podman socket" in said
    assert "the sandbox's browser starts on Cairn's board" in said


def test_other_models_per_worker_reach_the_relay(host):
    """"Cairn natürlich auch mit anderen Modellen"."""
    found, _ = plan(host, "claude", "--with", "codex", "--model", "sonnet", "--cairn", "--cairn-workers",
                    "codex=gpt-5.2-codex,claude")
    workers = {w["agent"]: w for w in found["cairn"]["workers"]}
    assert workers["codex"]["arguments"][-2:] == ["--model", "gpt-5.2-codex"] and \
        workers["codex"]["model"] == "gpt-5.2-codex"
    assert "analytics.enabled=false" in " ".join(workers["codex"]["arguments"]), "Codex's telemetry off as the lead's"
    assert workers["claude"]["arguments"] == ["--model", "sonnet"], "the lead's own model, as it was started"
    assert [w["type"] for w in found["cairn"]["dispatch"]["workers"]] == ["codex", "claudecode"]


def test_the_start_makes_cairn_s_files_and_the_relay_s_folder_and_removes_them(host, tmp_path):
    hold, tmp = tmp_path / "hold", tmp_path / "tmp"
    tmp.mkdir()
    started = host.popen("claude", "--with", "codex", "--cairn", "--cairn-origin", "The machine 10.0.0.5",
                         "--cairn-goal", "Its services", "--cairn-hint", "Port 80", "--no-tmux",
                         env={"FAKE_PODMAN_POD_HOLD": str(hold), "TMPDIR": str(tmp)})
    try:
        for _ in range(1200):
            if host.stopped_at() or started.poll() is not None:
                break
            time.sleep(0.05)
        assert host.stopped_at() is not None, started.communicate(timeout=60)
        runs = host.runtime / "sandbox-ai" / "runs"
        [relay] = list(runs.glob("*.cairn"))
        assert relay.is_dir() and relay.stat().st_mode & 0o777 == 0o700, "only the user's"
        [session] = [p for p in tmp.glob("agent-sandbox-*") if (p / "config.json").exists()]
        own = json.loads((session / "cairn.json").read_text())
        assert own["workers"] == ["claude", "codex"] and own["socket"] == "/sandbox-cairn/relay/relay.sock"
        assert own["project"] == {"title": "sandbox-ai: The machine 10.0.0.5", "origin": "The machine 10.0.0.5",
                                  "goal": "Its services", "hints": [{"content": "Port 80", "creator": "human"}]}
        assert own["dispatch"]["local"] == {"workspace_root": "/work/cairn/runs", "completed_action": "keep"}
        supervisor = json.loads((session / "config.json").read_text())
        assert {k: supervisor["cairn"][k] for k in ("socket", "workspace")} == {
            "socket": "/sandbox/cairn/relay.sock", "workspace": "/work/cairn/runs"}
        assert supervisor["cairn"]["workers"]["claude"] == [] and \
            all(word.startswith("-c") or "=" in word for word in supervisor["cairn"]["workers"]["codex"]), \
            "Codex: only its telemetry overrides"
        assert [w["env"] for w in own["dispatch"]["workers"]] == [{"SANDBOX_AI_CAIRN_WORKER": "claude"},
                                                                {"SANDBOX_AI_CAIRN_WORKER": "codex"}]
        assert [s["name"] for s in supervisor["services"]] == ["cairn", "browser"]
        assert supervisor["env"]["SANDBOX_AI_CAIRN"] == "http://127.0.0.1:8000"
        assert json.loads((session / "browser.json").read_text())["start_url"] == "http://127.0.0.1:8000/"
        [record] = [json.loads(p.read_text()) for p in runs.glob("*/run.json")]
        assert record["cairn"]["workers"] == ["claude", "codex"] and record["cairn"]["board"] == "http://127.0.0.1:8000"
    finally:
        hold.write_text("")
        started.communicate(timeout=120)
    assert not list((host.runtime / "sandbox-ai" / "runs").glob("*.cairn")), "the relay's folder goes with the run"


def test_the_pentest_preset_runs_cairn_s_part(host):
    found, _ = plan(host, "claude", "--preset", "cairn-pentest-board", "--preset-value", "target=10.10.10.5")
    assert [w["agent"] for w in found["cairn"]["workers"]] == ["claude", "codex"]
    assert found["cairn"]["project"]["origin"].startswith("The machine 10.10.10.5")
    assert found["browser"]["start_url"] == "http://127.0.0.1:8000/"


def test_the_local_preset_runs_the_workers_on_your_model(host):
    """"lokalem Preset supporten": the workers are the session's Claude Code on the --llm server."""
    added = host.run("llm", "add", "ollama", "--kind", "ollama", "--url", "http://127.0.0.1:11434", "--model",
                     "qwen3-coder")
    assert added.returncode == 0, added.stderr
    found, _ = plan(host, "claude", "--preset", "cairn-local-llm", "--preset-value", "llm=ollama")
    [worker] = found["cairn"]["workers"]
    assert worker["agent"] == "claude" and worker["model"] == "the --llm server's"
    assert found["cairn"]["project"] is None, "the project is made on the board"


@pytest.mark.parametrize("args, said", [
    (("claude", "--cairn", "--backend", "bwrap"), "--cairn needs the Podman backend"),
    (("claude", "--cairn-goal", "x"), "add --cairn"),
    (("claude", "--cairn", "--cairn-workers", "codex"), "add --with codex"),
    (("claude", "--cairn", "--cairn-workers", "pi"), "add --with pi"),
    (("claude", "--cairn", "--cairn-workers", "goose=m1", "--with", "goose"), "no model option"),
    (("claude", "--cairn", "--mode", "paranoid"), "--cairn"),
])
def test_what_is_refused_is_said(host, args, said):
    done = host.run(*args, "--dry-run")
    assert done.returncode != 0 and said in done.stderr, done.stderr


def test_without_its_image_the_start_names_the_build(tmp_path):
    """Its image is one of sandbox-ai's own: a start builds it first (0.1.96), which the dry run says; without the
    automatic build the start is refused with the build named."""
    host = F.Host(tmp_path)
    host.add_image("localhost/claude-sandbox:latest", BROWSER)
    done = host.run("claude", "--cairn", "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"the image {CAIRN} is not built for this user yet: the start builds it first (sandbox-ai build cairn)" \
        in done.stderr
    done = host.run("claude", "--cairn", "--dry-run", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"})
    assert done.returncode != 0 and "sandbox-ai build cairn" in done.stderr and CAIRN in done.stderr
