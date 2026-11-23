"""Analysis round 2 of 0.1.97 (user journeys end to end): what a user ran into walking the new features through the
real `./sandbox-ai` (tests/live_fakes.py: Podman, tmux, ssh and npm faked). Each test is named after what was broken
or misleading, and stays as a regression test."""
import json

import pytest

import live_fakes as F

AGENTS = ("claude", "codex", "opencode", "opencode2", "qwen", "goose", "dsh", "hermes", "letta", "nanobot",
          "openhands", "pi", "team")


@pytest.fixture
def host(tmp_path):
    found = F.Host(tmp_path)
    found.add_image(*[f"localhost/{agent}-sandbox:latest" for agent in AGENTS])
    found.add_image("localhost/sandbox-ai-cairn:latest", labels={"sandbox-ai.role": "cairn"})
    found.add_image("localhost/sandbox-ai-browser:latest", labels={"sandbox-ai.role": "browser"})
    return found


def dry(host, *args, env_file=False):
    done = host.run(args[0], "--dry-run", *args[1:], env={"SANDBOX_AI_NO_AUTO_BUILD": "1",
                                                          **({"SANDBOX_AI_SECRETS": "file"} if env_file else {})})
    return done, (json.loads(done.stdout) if done.returncode == 0 else None)


@pytest.mark.parametrize("lead", ["pi", "opencode2", "hermes", "qwen", "goose", "dsh", "letta", "nanobot",
                                  "openhands"])
def test_cairn_with_a_lead_outside_the_team_image_was_told_to_add_with(host, lead):
    """Journey: the Board's Cairn part beside a Pi (or OpenCode 2, Hermes ...) agent. The start was refused with
    "start claude or codex, or add --with claude or --with codex" - and following that advice was refused in turn:
    "--with: Pi is not in the team image". Since 0.1.98 every agent is in the team image (its layers); since every
    harness can be Cairn's worker (2026-11-22, "why workers only for claude and not all harnesses?") the lead alone
    is its worker, and --with claude still passes (the team image with that lead's layer)."""
    done, plan = dry(host, lead, "--cairn")
    if lead == "openhands":  # its own reason: its Canvas holds the port Cairn's board takes - never the team image
        assert done.returncode != 0 and "port 8000" in done.stderr and "team image" not in done.stderr
        return
    assert done.returncode == 0, done.stderr
    assert [w["agent"] for w in plan["cairn"]["workers"]] == [lead] and plan["cairn"]["workers"][0]["mapped"]
    host.add_image(f"localhost/team-{lead}-sandbox:latest")
    done, plan = dry(host, lead, "--with", "claude", "--cairn")
    assert done.returncode == 0, done.stderr
    assert plan["image"] == f"localhost/team-{lead}-sandbox:latest" and plan["companions"] == ["claude"]
    assert [w["agent"] for w in plan["cairn"]["workers"]] == ["claude", lead], "the best supported first"
    # Claude Code with --cairn passes as before.
    done, plan = dry(host, "claude", "--cairn")
    assert done.returncode == 0, done.stderr


def test_cairn_with_opencode_as_lead_is_its_own_worker_and_with_claude_still_works(host):
    done, plan = dry(host, "opencode", "--cairn")
    assert done.returncode == 0 and [w["agent"] for w in plan["cairn"]["workers"]] == ["opencode"], done.stderr
    done, _ = dry(host, "opencode", "--with", "claude", "--cairn")
    assert done.returncode == 0, done.stderr


def test_openhands_with_browser_started_on_a_blank_page(host):
    """Journey: `sandbox-ai openhands` says its Agent Canvas, its only interactive surface, runs on the sandbox's
    loopback and to start with --browser to see it. With --browser the sandbox's browser started on about:blank and
    nothing said where the Canvas is. The browser starts on the Canvas now (as it does on Cairn's board), on the port
    the Canvas is given, and the start says so; a task, which has no Canvas, starts it on nothing."""
    done, plan = dry(host, "openhands", "--browser")
    assert done.returncode == 0, done.stderr
    assert plan["browser"]["start_url"] == "http://127.0.0.1:8000/"
    assert "Agent Canvas" in done.stderr and "http://127.0.0.1:8000" in done.stderr, done.stderr
    done, plan = dry(host, "openhands", "--browser", "--", "--port", "9100")
    assert done.returncode == 0, done.stderr
    assert plan["browser"]["start_url"] == "http://127.0.0.1:9100/"
    done, plan = dry(host, "openhands", "--browser", "--", "task", "--text=hello")
    assert done.returncode == 0, done.stderr
    assert plan["browser"].get("start_url") is None
    # A browser only the agent drives shows the human nothing: no start page for the human either.
    done, plan = dry(host, "openhands", "--browser", "--browser-control", "agent")
    assert done.returncode == 0, done.stderr
    assert plan["browser"].get("start_url") is None


@pytest.mark.parametrize("agent", ["pi", "letta", "nanobot", "openhands"])
def test_llm_models_for_an_agent_given_one_model_said_nothing_and_its_plan_listed_the_others(host, agent):
    """Journey: the Board's LLM part beside Pi (Letta Code, nanobot, OpenHands) offers "Models offered in the agent";
    `--llm lm --llm-models a,b` started without a word, and the dry run's llm.models listed a and b as offered - but
    these agents' configuration names only the start's model (no way to switch is mapped for them yet). The start
    says so and how to take another; the plan lists the one model; OpenCode 2, which offers them, is unchanged."""
    assert host.run("llm", "add", "lm", "--kind", "lmstudio", "--url", "http://127.0.0.1:1234", "--model",
                    "qwen3-coder").returncode == 0
    done, plan = dry(host, agent, "--llm", "lm", "--llm-models", "gemma-3,llama-3")
    assert done.returncode == 0, done.stderr
    assert plan["llm"]["models"] == ["qwen3-coder"], plan["llm"]["models"]
    assert "--llm-models:" in done.stderr and "only the start's model qwen3-coder" in done.stderr, done.stderr
    assert "--llm lm:MODEL" in done.stderr
    done, plan = dry(host, agent, "--llm", "lm")
    assert done.returncode == 0 and "--llm-models:" not in done.stderr, done.stderr
    done, plan = dry(host, "opencode2", "--llm", "lm", "--llm-models", "gemma-3,llama-3")
    assert done.returncode == 0, done.stderr
    assert plan["llm"]["models"] == ["qwen3-coder", "gemma-3", "llama-3"]
    assert "only the start's model" not in done.stderr


def keep_secret(host, name):
    import subprocess
    done = subprocess.run(host.command("secrets", "set", name), input="sk-test-value\n", text=True,
                          capture_output=True, env={**host.env, "SANDBOX_AI_SECRETS": "file"}, cwd=host.workspace,
                          timeout=60)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("agent", ["claude", "codex", "opencode", "opencode2", "hermes", "dsh", "qwen", "goose",
                                   "openhands", "nanobot", "letta", "pi"])
def test_a_new_users_default_preset_reaches_its_model(host, agent):
    """Journey: a new user picks "AGENT (default)" in the gallery (or `--preset default-AGENT`), keeps the key it
    names, and starts. "OpenHands (default)" (and "Coding with OpenHands") reached no model at all - no --api-key,
    and OpenHands has no maker's service, so not one host was open; "nothing to fill in but its login or key" was
    not so. Every basic preset reaches its model once its key is kept (OpenHands: Anthropic's API by its key, as
    nanobot's, Letta Code's and Pi's), and with --browser OpenHands' Canvas is in the sandbox's browser."""
    shown = host.run("presets", "show", f"default-{agent}")
    options = next((line for line in shown.stdout.splitlines() if line.strip().startswith("options:")), "")
    for word in options.split():
        if "_API_KEY" in word and "=" in word:
            keep_secret(host, word.rsplit("=", 1)[1])
    done, plan = dry(host, agent, "--preset", f"default-{agent}", env_file=True)
    assert done.returncode == 0, done.stderr
    assert plan["https_hosts"], f"default-{agent} reaches no model"
    if agent == "openhands":
        assert plan["openhands"]["web_ui"] and "--browser to see it" in done.stderr
        done, plan = dry(host, agent, "--preset", "coding-openhands", env_file=True)
        assert done.returncode == 0 and "api.anthropic.com" in plan["https_hosts"], done.stderr
        done, plan = dry(host, agent, "--preset", "default-openhands", "--browser", env_file=True)
        assert done.returncode == 0 and plan["browser"]["start_url"] == "http://127.0.0.1:8000/", done.stderr


def test_a_helper_without_a_login_named_the_agents_with_one_without_pi(host):
    """Journey: `sandbox-ai pi --helper opencode2` without a key is refused with "take a helper with a login (claude,
    codex, opencode, hermes, dsh, qwen)" - Pi, which has a login (sandbox-ai login pi) and passes as a helper, was not
    among them. The list is every agent with a login."""
    done, _ = dry(host, "claude", "--helper", "opencode2")
    assert done.returncode != 0 and "take a helper with a login" in done.stderr, done.stderr
    named = done.stderr.split("take a helper with a login (", 1)[1].split(")", 1)[0].split(", ")
    assert set(named) == {"claude", "codex", "opencode", "hermes", "dsh", "qwen", "pi"}, named
    for agent in named:
        if agent != "claude":
            done, _ = dry(host, "claude", "--helper", agent)
            assert done.returncode == 0, (agent, done.stderr)
