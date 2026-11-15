"""Hotfix round 2 of 0.1.95: realistic user stories driven through the launcher as the user works (the real
./sandbox-ai entry point as a subprocess, live_fakes.py: a per-user Podman image store, fake tmux/npm/ssh). One test
per bug, named after what the user saw."""
import pytest

from live_fakes import Host


@pytest.fixture
def host(tmp_path):
    return Host(tmp_path)


# --- the first preset of the gallery's security category (paranoid, high) with a local model server ----------------

@pytest.mark.parametrize("mode", ["high", "paranoid"])
def test_a_local_model_server_in_a_strict_mode_is_refused_by_its_own_name(host, mode):
    """Story: the security presets (paranoid, canary-course: --mode-at-least high/paranoid) started with a local LLM
    server (sandbox-ai llm add ollama ... http://127.0.0.1:11434). The launcher refused with "--allow-host-port 11434
    widens the security mode ...; leave it out" - an option the user never gave (--llm opens the host's port behind
    the scenes). The refusal names --llm, what was given, and the mode that allows it."""
    added = host.run("llm", "add", "ollama", "--kind", "ollama", "--url", "http://127.0.0.1:11434", "--model",
                     "qwen3-coder:30b")
    assert added.returncode == 0, added.stderr
    result = host.run("claude", "--llm", "ollama", "--mode", mode, "--dry-run")
    assert result.returncode != 0
    said = result.stderr
    assert f"--llm ollama widens the security mode {mode}" in said, said
    assert "--allow-host-port" not in said, said
    assert "--mode medium" in said, said
    # Given by the user, --allow-host-port is still named as given.
    result = host.run("claude", "--allow-host-port", "8080", "--mode", mode, "--dry-run")
    assert "--allow-host-port 8080 widens the security mode" in result.stderr, result.stderr


# --- logs of a start by its name ----------------------------------------------------------------------------------

def test_logs_of_a_start_by_its_name_reads_its_log_directory(host):
    """Story: a start ended at once (here: Podman stopped it at its pod); the user, debugging it, typed what the
    launcher had named it - `sandbox-ai logs NAME` - and got "no log directory NAME": logs took only a folder. A name
    (or id) of `sandbox-ai list` now reads that start's own log directory; one that is neither says both."""
    import re
    host.add_image("localhost/claude-sandbox:latest")
    started = host.run("claude")
    name = re.search(r"sandbox-ai resume (\S+)", started.stderr).group(1)
    result = host.run("logs", name)
    assert "no log directory" not in result.stderr, result.stderr
    assert result.returncode == 0 or "nothing has run" in result.stderr, result.stdout + result.stderr
    result = host.run("logs", "no-such-start")
    assert result.returncode == 1
    assert "no log directory or start named no-such-start" in result.stderr, result.stderr
    assert "sandbox-ai list" in result.stderr


# --- tokens masked in logs: the forms the privacy check knows -------------------------------------------------------

def token_samples():
    """Tokens of the forms sandbox-ai's privacy check blocks in a push, assembled at run time (no scanner takes the
    file for real keys)."""
    import string
    tail = (string.ascii_letters + string.digits) * 2
    return {"Hugging Face": "hf" + "_" + tail[:34], "Google API": "AI" + "za" + tail[:35],
            "npm": "npm" + "_" + tail[:36], "PyPI": "pypi" + "-AgE" + tail[:60],
            "Stripe": "sk" + "_live_" + tail[:24]}


def test_the_masks_of_logs_know_the_tokens_the_privacy_check_knows():
    """Story: data-ml and Goose local inference reach Hugging Face; a token of it in a build's output or in a request
    the connection proxy logs stayed readable - the logs' masks knew GitHub, GitLab, OpenAI, Slack and AWS tokens, the
    privacy check of pushes also Hugging Face, Google API, npm, PyPI and Stripe ones. Both masks know them all now
    (the app's bug report, test_hotfix_round_2 of the app, too)."""
    import importlib.util
    from live_fakes import ROOT

    def load(name):
        spec = importlib.util.spec_from_file_location(f"hotfix2_{name}", ROOT / "sandbox" / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    progress, runtime = load("progress"), load("runtime")
    for kind, token in token_samples().items():
        line = f"downloading with {token} now"
        assert token not in progress.scrub(line), kind
        assert token not in runtime.redact_text(line, anchored=False), kind
        assert token not in runtime.redact_text(line + " " + "x" * 3000), kind  # (a long body: searched anchored)


# --- a start through Tor on a host without the Tor image ------------------------------------------------------------

@pytest.mark.parametrize("kind,image", [("tor", "localhost/sandbox-ai-tor:latest")])
def test_a_start_through_tor_builds_the_tor_image_it_lacks_as_it_builds_the_agents(host, kind, image):
    """Story: the Tor presets (tor, showcase-vpn, network-maze; the app's vpn-all, tailnet) started from the Board on
    a host where only the agent's image was there. The Board's check accepted, the start prepared everything and
    then ended: "The Tor image localhost/sandbox-ai-tor:latest is missing; build it with: sandbox-ai build tor" -
    while a missing agent image is built by the start itself ("not built for this user yet: building it now"). The
    Tor (and VPN) image is sandbox-ai's own too: the start builds it the same way, as this user, and goes on."""
    host.add_image("localhost/claude-sandbox:latest")
    assert host.run("vpn", "add", "tor-net", "--tor").returncode == 0
    result = host.run("claude", "--vpn", "tor-net")
    said = result.stderr
    assert "is missing; build it with" not in said, said
    assert f"the image {image} is not built for this user yet: building it now (sandbox-ai build {kind}" in said, said
    assert image in host.images()
    assert host.stopped_at() is not None, "the start goes on to its pod"
    # As root it would go into root's store (the user's report): the build ran as the user, into their store.
    assert not [call for call in host.builds() if "sudo" in call]


# --- approvals while a start waits for an answer -----------------------------------------------------------------

def test_approvals_alone_lists_the_open_questions(host):
    """Story: a start waits for an answer (a connection the options do not allow); the user, in another terminal,
    typed `sandbox-ai approvals` - and got "error: the following arguments are required: command". Alone (or with
    --json) it lists the open questions, as `sandbox-ai kits` alone lists the kits."""
    import json
    listed = host.run("approvals", "list")
    alone = host.run("approvals")
    assert alone.returncode == 0, alone.stderr
    assert alone.stdout == listed.stdout and "required" not in alone.stderr
    as_json = host.run("approvals", "--json")
    assert as_json.returncode == 0, as_json.stderr
    assert json.loads(as_json.stdout) == json.loads(host.run("approvals", "list", "--json").stdout)


LISTED_ALONE = ["mcp", "vpn", "llm", "secrets", "identities", "hosts", "presets", "devices", "canaries", "net",
                "git-logins", "publish-logins"]


@pytest.mark.parametrize("command", LISTED_ALONE)
def test_a_registry_command_alone_lists_as_images_kits_and_defaults_do(host, command):
    """Story, checking what is there before a start: `sandbox-ai mcp`, `vpn`, `llm`, `presets`... alone said
    "error: the following arguments are required: command", while `images`, `kits`, `plugins` and `defaults` alone
    list. Each command that has a list lists when it is given alone."""
    alone, listed = host.run(command), host.run(command, "list")
    assert "the following arguments are required" not in alone.stderr, alone.stderr
    assert (alone.returncode, alone.stdout) == (listed.returncode, listed.stdout)


# --- the browser beside a Tor start, its image tag taken by another image ---------------------------------------------

def test_a_browser_image_that_is_not_sandbox_ais_is_said_as_such_not_as_missing(host):
    """Story: a Tor start with the browser (--vpn tor-net --browser chromium --mcp duckduckgo). The tag
    localhost/sandbox-ai-browser:latest held an image without sandbox-ai's label (another build, an old one): the
    start neither built its own (the tag was there) nor took that one, and said "is not there ... build it once" -
    while `podman images` listed it. It says that the image there is not sandbox-ai's own, and the build replaces it."""
    host.add_image("localhost/claude-sandbox:latest", "localhost/sandbox-ai-browser:latest")
    result = host.run("claude", "--browser", "chromium")
    said = result.stderr
    assert result.returncode != 0
    assert "is not sandbox-ai's own" in said and "sandbox-ai build browser" in said, said
    assert "is not there" not in said, said


def test_the_tor_image_is_built_before_the_mcp_servers_behind_tor_start_and_the_check_says_so(host):
    """The same story with tor-research-v2's MCP servers behind Tor: they start before the agent's own VPN, so the
    Tor image must be there (built) before them - a server behind a Tor of its own, without --vpn for the agent,
    too. The Board's check (a dry run) says that the start builds it, as for the agent's image."""
    import json
    host.add_image("localhost/claude-sandbox:latest")
    assert host.run("vpn", "add", "tor-net", "--tor").returncode == 0
    checked = host.run("claude", "--vpn", "tor-net", "--dry-run")
    assert checked.returncode == 0, checked.stderr
    assert "the image localhost/sandbox-ai-tor:latest is not built for this user yet: the start builds it first " \
           "(sandbox-ai build tor)" in checked.stderr, checked.stderr
    assert host.builds() == []
    added = host.run("mcp", "add", "mcp:duckduckgo", "--network", "full", "--vpn", "tor-net", "--no-pull")
    assert added.returncode == 0, added.stderr
    host.add_image(json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["duckduckgo"]["image"])
    started = host.run("claude", "--mcp", "duckduckgo")
    said = started.stderr
    building = said.find("building it now (sandbox-ai build tor")
    assert building >= 0, said
    server = said.find("starting the MCP server duckduckgo")
    assert server < 0 or building < server, said
    assert "localhost/sandbox-ai-tor:latest" in host.images()
