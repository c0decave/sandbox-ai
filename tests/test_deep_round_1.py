"""Deep test round 1 (features of 0.1.93, 0.1.94 and 0.1.95): what a hostile, careless or impatient user ran into,
end to end through the real ./sandbox-ai entry point with only Podman, tmux and npm faked (live_fakes.py), each bug a
test named after it.
"""
import json

import pytest

from live_fakes import Host


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    h.env.pop("SANDBOX_AI_RUN", None)  # (this suite may run inside a sandbox-ai run)
    return h


# --- image variants per agent: a variant's name is part of an image name Podman must accept -------------------------

@pytest.mark.parametrize("name", ["x-", "a..b", "a___b", "trail.", "under_", "a.-b"])
def test_a_variant_name_podman_cannot_tag_is_refused_when_it_is_made(host, name):
    """`images new x- --from claude` made a variant whose image localhost/claude-sandbox-x-:latest is no valid image
    reference: every build of it failed in Podman ("invalid reference format"). Refused at once, with why."""
    result = host.run("images", "new", name, "--from", "claude")
    assert result.returncode != 0, result.stdout
    assert "Traceback" not in result.stderr
    assert "image name" in result.stderr or "letters" in result.stderr, result.stderr
    assert not (host.home / ".config/sandbox-ai/images/variants/claude" / name).exists()


@pytest.mark.parametrize("name", ["x-", "a..b", "a___b"])
def test_an_image_name_of_yours_podman_cannot_tag_is_refused(host, tmp_path, name):
    """`images build-image x- --file Containerfile` tagged localhost/sandbox-ai-image-x-:latest: no valid image
    reference. Refused before anything is pulled or built."""
    containerfile = tmp_path / "ctx" / "Containerfile"
    containerfile.parent.mkdir()
    containerfile.write_text("FROM scratch\n")
    result = host.run("images", "build-image", name, "--file", containerfile)
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert not host.builds(), host.calls()


@pytest.mark.parametrize("name", ["ok", "a.b", "a_b", "a__b", "a-b", "a--b", "v1.2-x_y"])
def test_variant_names_podman_takes_are_still_made(host, name):
    result = host.run("images", "new", name, "--from", "claude")
    assert result.returncode == 0, result.stderr
    assert f"localhost/claude-sandbox-{name}:latest" in result.stdout


# --- MCP variables of a start (--mcp-env) end to end ------------------------------------------------------------

MCP_IMAGE = "ghcr.io/x/srv@sha256:" + "a" * 64


@pytest.fixture
def mcp_host(host):
    """A host with the image server srv in mcp.json: FLAG=1 a value, API_TOKEN from the secret MYTOKEN, OPT optional."""
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    (config / "mcp.json").write_text(json.dumps({"srv": {
        "image": MCP_IMAGE, "network": "none", "env_values": {"FLAG": "1"}, "env_secrets": {"API_TOKEN": "MYTOKEN"},
        "env_optional": ["OPT"]}}))
    host.add_image(MCP_IMAGE)
    host.env["SANDBOX_AI_SECRETS"] = "file"
    return host


@pytest.mark.parametrize("variable", ["API_TOKEN", "DB_PASSWORD"])
def test_mcp_env_refusal_of_a_secret_value_names_the_start_option(mcp_host, variable):
    """`--mcp-env srv:DB_PASSWORD=x` was refused with 'give --env DB_PASSWORD (or --env DB_PASSWORD@SECRET ...)' -
    options of `mcp add`, not of the start that was refused. It names --mcp-env srv:DB_PASSWORD@SECRET."""
    result = mcp_host.run("claude", "--dry-run", "--mcp", "srv", "--mcp-env", f"srv:{variable}=x")
    assert result.returncode == 1
    assert f"--mcp-env srv:{variable}@SECRET" in result.stderr, result.stderr
    assert f"give --env {variable}" not in result.stderr


def test_mcp_env_end_to_end_values_secrets_and_refusals(mcp_host):
    secret = subprocess_input(mcp_host, ["secrets", "set", "MYTOKEN"], "s3cr3t-value-xyz\n")
    assert secret.returncode == 0, secret.stderr
    plain = mcp_host.run("claude", "--dry-run", "--mcp", "srv")
    assert plain.returncode == 0, plain.stderr
    own = mcp_host.run("claude", "--dry-run", "--mcp", "srv", "--mcp-env", "srv:FLAG=2", "--mcp-env", "srv:OPT=x")
    assert own.returncode == 0, own.stderr
    assert "s3cr3t-value-xyz" not in own.stdout + own.stderr
    plan = json.loads(own.stdout)
    assert [entry["instance"].split(".")[0] for entry in plan["mcp"]] == ["srv"]
    assert plan["mcp"][0]["instance"] != "srv", plan["mcp"]  # an instance of its own
    assert json.loads(plain.stdout)["mcp"][0]["instance"] == "srv"
    for value, said in (("srv:FLAG", "give its value"), ("other:FLAG=1", "does not use the MCP server other"),
                        ("srv:SANDBOX_AI_MCP_MOUNTS=x", "sets this variable itself"), (":X=1", "NAME:VAR=VALUE")):
        result = mcp_host.run("claude", "--dry-run", "--mcp", "srv", "--mcp-env", value)
        assert said in result.stderr, (value, result.stderr)
        assert "Traceback" not in result.stderr


BINWALK = "hub:binwalk"


def folder_cases(host):
    return {"home": host.home, "own": host.home / ".config/sandbox-ai"}


@pytest.mark.parametrize("case", ["home", "own"])
def test_mcp_add_refuses_a_folder_the_server_could_never_start_with(host, case):
    """`mcp add hub:binwalk --mount /work=$HOME` added the server (and pulled its image); every start then failed late,
    after the workspace was made, with 'is or holds ... sandbox-ai's runs (their control sockets)'. Refused at once:
    the launcher's host-path policy for a server's folders, read-only too."""
    folder = folder_cases(host)[case]
    folder.mkdir(parents=True, exist_ok=True)
    (host.home / ".local/state/sandbox-ai/runs").mkdir(parents=True, exist_ok=True)
    result = host.run("mcp", "add", BINWALK, "--no-pull", "--mount", f"/work={folder}")
    assert result.returncode == 1, result.stdout
    assert "Traceback" not in result.stderr
    assert "sandbox-ai's" in result.stderr, result.stderr
    assert not (host.home / ".config/sandbox-ai/mcp.json").exists() or \
        "binwalk" not in json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())


@pytest.mark.parametrize("case", ["home", "own"])
def test_a_start_whose_mcp_folder_breaks_the_policy_is_refused_before_anything_is_made(host, tmp_path, case):
    """`claude --mcp binwalk --mcp-mount binwalk:/work=$HOME`: the dry run said all was well, and the start went on to
    the workspace and an instance folder before the server refused it. Now among the problems checked first."""
    project = tmp_path / "project"
    project.mkdir()
    assert host.run("mcp", "add", BINWALK, "--no-pull", "--mount", f"/work={project}").returncode == 0
    image = json.loads((host.home / ".config/sandbox-ai/mcp.json").read_text())["binwalk"]["image"]
    host.add_image(image)
    folder = folder_cases(host)[case]
    (host.home / ".local/state/sandbox-ai/runs").mkdir(parents=True, exist_ok=True)
    dry = host.run("claude", "--dry-run", "--mcp", "binwalk", "--mcp-mount", f"binwalk:/work={folder}")
    assert dry.returncode == 1 and "sandbox-ai's" in dry.stderr, dry.stderr
    start = host.run("claude", "--mcp", "binwalk", "--mcp-mount", f"binwalk:/work={folder}")
    assert start.returncode == 1 and "sandbox-ai's" in start.stderr, start.stderr
    assert "starting the MCP server" not in start.stderr
    assert not list((host.home / ".local/state/sandbox-ai/mcp").glob("binwalk.*"))
    fine = host.run("claude", "--dry-run", "--mcp", "binwalk")
    assert fine.returncode == 0, fine.stderr


def subprocess_input(host, args, text):
    import subprocess
    return subprocess.run(host.command(*args), cwd=host.workspace, env=host.env, input=text, capture_output=True,
                          text=True, timeout=120)


# --- the LLM traffic log: sandbox-ai llm traffic end to end -------------------------------------------------------

def traffic_log(directory, run="r1", count=3):
    directory.mkdir(parents=True, exist_ok=True)
    lines = [{"version": 1, "n": n, "run": run, "time": f"2026-11-13T10:00:0{n}.000Z", "api": "chat", "model": "m",
              "status": 200, "ms": 100 * n, "request": {"messages": [{"role": "user", "content": f"question {n}"}]},
              "response": {"text": f"answer {n}", "finish_reason": "stop"}} for n in range(1, count + 1)]
    path = directory / f"llm-traffic-20261113-100000-{run}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines) + "{damaged\n" + json.dumps(lines[0])[:30])
    return path


def test_llm_traffic_markdown_of_a_request_that_is_not_there_says_so(host, tmp_path):
    """`llm traffic --markdown --request 9` printed an empty report with exit status 0, where --summary, --json and
    the chat view say 'no request 9'."""
    traffic_log(tmp_path / "logs")
    result = host.run("llm", "traffic", "--log-dir", tmp_path / "logs", "--markdown", "--request", "9")
    assert result.returncode == 1 and "no request 9" in result.stderr, (result.stdout, result.stderr)
    found = host.run("llm", "traffic", "--log-dir", tmp_path / "logs", "--markdown", "--request", "2")
    assert found.returncode == 0 and "question 2" in found.stdout and "question 1" not in found.stdout


def test_llm_traffic_end_to_end_every_view_skips_a_damaged_line(host, tmp_path):
    traffic_log(tmp_path / "logs")
    logs = tmp_path / "logs"
    chat = host.run("llm", "traffic", "--log-dir", logs)
    assert chat.returncode == 0 and all(f"answer {n}" in chat.stdout for n in (1, 2, 3)), chat.stderr
    summary = host.run("llm", "traffic", "--log-dir", logs, "--summary", "--json")
    assert [json.loads(line)["n"] for line in summary.stdout.splitlines()] == [1, 2, 3]
    tail = host.run("llm", "traffic", "--log-dir", logs, "--tail", "1", "--summary", "--json")
    assert [json.loads(line)["n"] for line in tail.stdout.splitlines()] == [3]
    grep = host.run("llm", "traffic", "--log-dir", logs, "--grep", "QUESTION 2", "--json")
    assert [json.loads(line)["n"] for line in grep.stdout.splitlines()] == [2]
    runs = host.run("llm", "traffic", "--log-dir", logs, "--runs", "--json")
    assert [r["run"] for r in json.loads(runs.stdout)] == ["r1"]
    markdown = host.run("llm", "traffic", "--log-dir", logs, "--markdown")
    assert markdown.stdout.startswith("# LLM traffic of r1") and "### Answer (stop)" in markdown.stdout
    for result in (chat, summary, tail, grep, runs, markdown):
        assert "Traceback" not in result.stderr


# --- telemetry always off, default presets, stop --all: end to end (features without an e2e test) -----------------

AGENTS = ("claude", "codex", "opencode", "hermes", "dsh", "qwen", "goose")


@pytest.mark.parametrize("agent", AGENTS)
def test_telemetry_on_is_ignored_with_a_note_and_the_run_is_off(host, agent):
    result = host.run(agent, "--dry-run", "--telemetry", "on")
    assert result.returncode == 0, result.stderr
    assert "telemetry is always off since 0.1.95; 'on' is ignored" in result.stderr
    plan = json.loads(result.stdout)
    assert plan["telemetry"]["level"] == "off"
    if agent in ("hermes", "dsh", "qwen", "goose"):  # never switchable: pinned, their hosts refused by the proxy
        assert plan[agent]["telemetry"]["switchable"] is False
        assert "telemetry" in [source["name"] for source in plan["rulesets"]]
    strict = json.loads(host.run(agent, "--dry-run", "--telemetry", "strict").stdout)
    assert strict["telemetry"]["level"] == "strict"


def test_telemetry_on_cannot_be_a_default(host):
    result = host.run("defaults", "set", "telemetry", "on")
    assert result.returncode != 0 and "always off" in result.stderr
    assert host.run("defaults", "set", "telemetry", "strict").returncode == 0
    plan = json.loads(host.run("claude", "--dry-run").stdout)
    assert plan["telemetry"]["level"] == "strict"


def test_a_defaults_file_of_0_1_94_with_telemetry_on_is_shown_as_ignored(host):
    """A defaults.json written before 0.1.95 may say "telemetry": "on": a start ignores it with a note, but `defaults
    show` listed it as if it held ("on"), without a word."""
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True)
    (config / "defaults.json").write_text(json.dumps({"version": 1, "telemetry": "on"}))
    start = host.run("claude", "--dry-run")
    assert start.returncode == 0 and json.loads(start.stdout)["telemetry"]["level"] == "off"
    assert "'on' is ignored" in start.stderr
    shown = host.run("defaults", "show")
    line = next(line for line in shown.stdout.splitlines() if line.strip().startswith("telemetry"))
    assert "ignored" in line and "always off" in line, line
    assert host.run("defaults", "unset", "telemetry").returncode == 0


@pytest.mark.parametrize("agent", AGENTS)
def test_every_agent_has_its_default_preset(host, agent):
    result = host.run("presets", "show", f"default-{agent}")
    assert result.returncode == 0, result.stderr
    assert f"agents: {agent}" in result.stdout and "telemetry is always off" in result.stdout


def test_stop_all_is_refused_inside_a_sandbox_and_takes_name_or_all(host):
    inside = host.run("stop", "--all", "--yes", env={"SANDBOX_AI_RUN": "outer-run"})
    assert inside.returncode == 1 and "refused inside a sandbox" in inside.stderr
    both = host.run("stop", "x", "--all")
    assert both.returncode != 0 and "stop takes NAME, or --all" in both.stderr
    yes = host.run("stop", "x", "--yes")
    assert yes.returncode != 0 and "--yes goes with --all" in yes.stderr
    for result in (inside, both, yes):
        assert "Traceback" not in result.stderr


def test_a_variant_made_for_another_agent_brings_its_files(host, tmp_path):
    """A variant of claude with a file (images extend v --add-file) made for dsh too (a preset or board switched to
    dsh: images.adopt_variant) copied its image.json and fragment but not its files/ folder: the copy named a file
    it did not have, and `images check` / its build failed."""
    source = tmp_path / "tool.txt"
    source.write_text("hello\n")
    assert host.run("images", "new", "v", "--from", "claude").returncode == 0
    added = host.run("images", "extend", "v", "--add-file", f"{source}:/usr/local/share/tool.txt")
    assert added.returncode == 0, added.stderr
    start = host.run("dsh", "--image-variant", "v")  # (stops where the pod would be made)
    assert "Traceback" not in start.stderr
    copy = host.home / ".config/sandbox-ai/images/variants/dsh/v"
    assert (copy / "image.json").is_file(), start.stderr
    assert (copy / "files").is_dir() and [p.read_text() for p in (copy / "files").iterdir()] == ["hello\n"]
    check = host.run("images", "check", "v@dsh")
    assert check.returncode == 0, check.stdout + check.stderr


@pytest.fixture
def service_host(host):
    """A host with an image of yours, pg (pulled, pinned by digest), in its Podman store."""
    config = host.home / ".config/sandbox-ai"
    config.mkdir(parents=True, exist_ok=True)
    image = "docker.io/library/postgres@sha256:" + "b" * 64
    (config / "user-images.json").write_text(json.dumps({"version": 1, "images": {"pg": {
        "kind": "pulled", "id": "sha256:" + "c" * 64, "image": image, "user": "999:999", "ports": [5432], "os": {}}}}))
    host.add_image(image)
    return host


def test_services_end_to_end_the_plan_and_every_refusal(service_host):
    ok = service_host.run("claude", "--dry-run", "--service", "db:image=pg,port=5432")
    assert ok.returncode == 0, ok.stderr
    plan = json.loads(ok.stdout)
    assert [(s["name"], s["image"], s["user"]) for s in plan["services"]] == [("db", "pg", "999:999")]
    assert "UNCHECKED" in ok.stderr and "localhost:5432" in ok.stderr
    refused = {("--service", "db:image=pg,port=5432", "--service", "cache:image=pg,port=5432"): "the service db's too",
               ("--service", "db:image=pg,port=9222", "--browser", "--browser-control", "agent"): "DevTools port",
               ("--service", "db:image=pg,port=53"): "the sandbox's DNS",
               ("--service", "db:image=pg,ready=5433,port=5432"): "none of its ports",
               ("--service", "db:image=pg,port=5432", "--allow-host-port", "5432"): "a port of the host",
               ("--service", "db:image=pg,port=5432", "--backend", "bwrap"): "needs the Podman backend",
               ("--service", "db:image=pg,port=5432", "--mode", "paranoid"): "widens the security mode paranoid",
               ("--service", "db:image=nope"): "no image nope of yours"}
    for args, said in refused.items():
        result = service_host.run("claude", "--dry-run", *args)
        assert result.returncode != 0 and said in result.stderr, (args, result.stderr)
        assert "Traceback" not in result.stderr


def load(name):
    from importlib import util
    from live_fakes import ROOT
    spec = util.spec_from_file_location(f"{name}_deep1", ROOT / f"sandbox/{name}.py")
    module = util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PROXY_NOTE = "passes the proxy, logged with credentials redacted"


@pytest.mark.parametrize("option", ["--no-proxy", "--offline"])
def test_the_browser_note_does_not_promise_a_proxy_the_run_has_not(host, option):
    """`claude --browser --no-proxy` said 'what you type in it (passwords too) passes the proxy, logged with
    credentials redacted' - with no proxy at all (and with --offline no network)."""
    result = host.run("claude", "--dry-run", "--browser", option)
    assert result.returncode == 0, result.stderr
    assert "browser:" in result.stderr
    assert PROXY_NOTE not in result.stderr, result.stderr
    assert ("no connection proxy" if option == "--no-proxy" else "offline") in result.stderr, result.stderr


def test_the_browser_note_names_the_proxy_where_there_is_one(host):
    result = host.run("claude", "--dry-run", "--browser")
    assert result.returncode == 0 and PROXY_NOTE in result.stderr, result.stderr


def test_the_default_name_of_a_long_repository_is_one_podman_can_tag():
    """A pulled image's default name (its repository's last part, cut to 30) could end in '-' once cut: the pull
    without --as was then refused by its own default."""
    services = load("services")
    for path in ("org/abcdefghijklmnopqrstuvwxyz012-x", "library/sandbox", "org/abcdefghijklmnopqrstuvwxy__z"):
        name = services.default_name({"path": path})
        assert services.images_module().taggable(name), name
