"""ARTEX as a part of its own (--artex), end to end: the real ./sandbox-ai entry point as a user runs it, with only
Podman, tmux and npm faked (tests/live_fakes.py). The dry run shows ARTEX's container beside the agent, its loopback
listeners, the console's forward and its LLM; a start makes its configuration (no secret in it), its folder only the
user's and its container's podman line (the password only in podman's environment); the preset expands to it; what
the launcher refuses is said.

The user: "check if we can integrate artex ... but check for backdoors of it. if none we add it." and "they claim they
have removed the backdoors spreaded with other versions, we still have to be careful"."""
import json
from pathlib import Path
import time

import pytest

import live_fakes as F

ARTEX = "localhost/sandbox-ai-artex:latest"


@pytest.fixture
def host(tmp_path):
    found = F.Host(tmp_path)
    found.add_image("localhost/claude-sandbox:latest", "localhost/codex-sandbox:latest")
    found.add_image(ARTEX, labels={"sandbox-ai.role": "artex"})
    return found


def plan(host, *args):
    done = host.run(*args, "--dry-run")
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout), done.stderr


def test_the_dry_run_shows_artex_beside_the_agent_on_the_loopback(host):
    found, said = plan(host, "claude", "--artex", "--allow-tcp", "10.10.10.5:80")
    artex = found["artex"]
    assert artex["container"].endswith("-service-artex") and artex["image"] == ARTEX and artex["user"] == "1000:1000"
    assert artex["commit"] == "a91b27feb7168c1486f827559bc48a589a920012"
    assert artex["tree"] == "22083e0bb9db66b76b94814f368d0d1dc5c3adbb" and artex["licence"] == "AGPL-3.0"
    assert artex["listens"] == ["127.0.0.1:8787 (console and API)", "127.0.0.1:8788 (its recording proxy)"]
    assert artex["console"]["host"] == "http://127.0.0.1:8787" and artex["console"]["user"] == "ARTEX"
    assert "no port of the pod is published" in artex["console"]["how"]
    assert artex["read_only"] and "none" in artex["capabilities"] and "removed at the build" in artex["self_update"]
    assert artex["data"]["kind"] == "fresh" and "tmpfs" in artex["data"]["where"]
    # Section 9.6: no host folder beyond its data: only read-only files of sandbox-ai's.
    assert set(artex["host_files"]) <= {"/etc/resolv.conf", "/etc/hosts", "/sandbox-artex/artex_runtime.py",
                                        "/sandbox-artex/config.json", "/sandbox-artex/ca"}
    assert "configure it in ARTEX's console" in artex["llm"]
    assert "ARTEX in its own container beside the agent" in said and "sandbox-ai artex console" in said
    assert "its targets are only what this start's allow-list opens" in said


def test_with_llm_artex_gets_the_server_and_a_placeholder_never_a_key(host):
    added = host.run("llm", "add", "lab", "--kind", "ollama", "--url", "http://127.0.0.1:11434", "--model", "qwen3")
    assert added.returncode == 0, added.stderr
    found, said = plan(host, "claude", "--artex", "--llm", "lab")
    llm = found["artex"]["llm"]
    assert llm == {"ARTEX_LLM_PROVIDER": "openai", "ARTEX_LLM_BASE_URL": "http://127.0.0.1:11434/v1",
                   "ARTEX_LLM_MODEL": "qwen3", "OPENAI_API_KEY": "sandbox-ai-llm-key-placeholder"}
    assert "its LLM is the --llm server" in said


def test_kept_data_is_a_volume_of_the_session(host):
    found, said = plan(host, "claude", "--artex", "--artex-data", "session", "--session", "lab")
    assert found["artex"]["data"]["kind"] == "session" and "sandbox-ai-svc-" in found["artex"]["data"]["where"]
    assert "kept with the session" in said


def test_the_start_makes_artex_s_files_its_folder_and_its_container_line(host, tmp_path):
    hold, tmp = tmp_path / "hold", tmp_path / "tmp"
    tmp.mkdir()
    started = host.popen("claude", "--artex", "--allow-tcp", "10.10.10.5:80", "--no-tmux",
                         env={"FAKE_PODMAN_POD_HOLD": str(hold), "TMPDIR": str(tmp)})
    try:
        for _ in range(1200):
            if host.stopped_at() or started.poll() is not None:
                break
            time.sleep(0.05)
        assert host.stopped_at() is not None, started.communicate(timeout=60)
        runs = host.runtime / "sandbox-ai" / "runs"
        [folder] = list(runs.glob("*.artex"))
        assert folder.is_dir() and folder.stat().st_mode & 0o777 == 0o700, "only the user's"
        [session] = [p for p in tmp.glob("agent-sandbox-*") if (p / "config.json").exists()]
        own = json.loads((session / "artex.json").read_text())
        assert own == {"port": 8787, "proxy_port": 8788, "data": "/var/lib/artex", "llm": {}}
        supervisor = json.loads((session / "config.json").read_text())
        [entry] = [s for s in supervisor["services"] if s["name"] == "artex"]
        assert entry["ready"] == 8787 and entry.get("ca") is True
        assert supervisor["env"]["SANDBOX_AI_ARTEX"] == "http://127.0.0.1:8787"
        assert not any("ARTEX_PASSWORD" in key for key in supervisor["env"]), "never the agent's"
        [record] = [json.loads(p.read_text()) for p in runs.glob("*/run.json")]
        assert record["artex"]["server"] == "http://127.0.0.1:8787"
        assert record["artex"]["console"] == "sandbox-ai artex console"
    finally:
        hold.write_text("")
        started.communicate(timeout=120)
    assert not list((host.runtime / "sandbox-ai" / "runs").glob("*.artex")), "its folder goes with the run"


def test_the_pentest_preset_asks_the_target_and_opens_only_it(host):
    found, _ = plan(host, "claude", "--preset", "artex-pentest", "--preset-value", "target=10.10.10.5")
    assert found["artex"]["container"].endswith("-service-artex")
    assert found["allow_tcp"] == ["10.10.10.5:80"], "the scope: exactly the target's port"
    done = host.run("claude", "--preset", "artex-pentest", "--dry-run")
    assert done.returncode != 0 and "target" in done.stderr, done.stderr


@pytest.mark.parametrize("args, said", [
    (("claude", "--artex", "--backend", "bwrap"), "--artex needs the Podman backend"),
    (("claude", "--artex-data", "session"), "add --artex"),
    (("claude", "--artex-port", "9000"), "add --artex"),
    (("claude", "--artex", "--artex-port", "80"), "1024 to 65535"),
    (("claude", "--artex", "--mode", "paranoid"), "--artex"),
    (("claude", "--artex", "--allow-host-port", "8787"), "port 8787"),
])
def test_what_is_refused_is_said(host, args, said):
    done = host.run(*args, "--dry-run")
    assert done.returncode != 0 and said in done.stderr, done.stderr


def test_without_its_image_the_start_names_the_build(tmp_path):
    host = F.Host(tmp_path)
    host.add_image("localhost/claude-sandbox:latest")
    done = host.run("claude", "--artex", "--dry-run")
    assert done.returncode == 0, done.stderr
    assert f"the image {ARTEX} is not built for this user yet: the start builds it first (sandbox-ai build artex)" \
        in done.stderr
    done = host.run("claude", "--artex", "--dry-run", env={"SANDBOX_AI_NO_AUTO_BUILD": "1"})
    assert done.returncode != 0 and "sandbox-ai build artex" in done.stderr and ARTEX in done.stderr


def test_the_console_command_says_where_and_the_password(host):
    runs = host.runtime / "sandbox-ai" / "runs"
    (runs / "brisk-otter").mkdir(parents=True)
    (runs / "brisk-otter" / "run.json").write_text(json.dumps({"name": "lab", "artex": {"server": "x"}}))
    (runs / "brisk-otter.artex").mkdir(mode=0o700)
    (runs / "brisk-otter.artex" / "console.json").write_text(json.dumps(
        {"url": "http://127.0.0.1:8788", "user": "ARTEX", "password": "pw-1234567890", "container": "c"}))
    done = host.run("artex", "console")
    assert done.returncode == 0, done.stderr
    assert "http://127.0.0.1:8788" in done.stdout and "pw-1234567890" in done.stdout and "ARTEX" in done.stdout
    done = host.run("artex", "console", "other")
    assert done.returncode == 1 and "no running start with ARTEX's console named other" in done.stderr
