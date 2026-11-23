"""presets-live (scripts/live_check.sh --only presets-live) and its evidence, without a container engine: the user,
2026-11-22: "mir scheint das alle presets auch noch nicht getestet sind" - every built-in preset started for real.

Here (no Podman, no Bubblewrap): every preset's plan - what values the check gives it, what it skips and why, with the
command to run it yourself - and a dry run of every planned start through the real ./sandbox-ai with both backends it
may use; the live test itself (tests/test_live_gaps.py::test_live_preset_smoke_run) with a stand-in launcher; the
report's table; scripts/import_live_report.py into sandbox/live_evidence.json; and the presets' live light in
`sandbox-ai presets list` and `show` (green with its date and version, red with its reason, nothing without data)."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SAI = ROOT / "sandbox-ai"
sys.path.insert(0, str(ROOT / "tests"))
import toolchain_fakes as TF  # noqa: E402


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LG = load("live_gaps_for_presets_live", ROOT / "scripts" / "live_gaps.py")
IMPORT = load("import_live_report_for_tests", ROOT / "scripts" / "import_live_report.py")
P = load("presets_for_presets_live", ROOT / "sandbox" / "presets.py")
PRESETS = LG.builtin_presets()
LLM = "studio:qwen3.8-flash-next"
# What only the user can give (2026-11-22's presets): the reason each of these is skipped without --preset-values.
NEEDS_YOURS = {"cairn-pentest-board": "a value of yours: target", "pentest-lab": "a value of yours: target",
               "artex-pentest": "a value of yours: target",
               "archivist-1995": "display (--gui)", "onboarding-buddy": "display (--gui)",
               "pixel-art-factory": "display (--gui)", "web-frontend": "display (--gui)",
               "radio-sandbox-ai": "sound (--audio)", "android-development": "an image variant of yours",
               "paranoid": "asks you before every action"}
# What a start with --llm refuses by design: these run with --provider-login (the agent's own provider).
LLM_REFUSED = {**{name: "--mode high" for name in ("canary-course", "code-audit", "forensics", "observer",
                                                   "pair-reviewer")},
               "hermes-claude": "--helper", "showcase-second-opinion": "--helper",
               "sand-chess": "--with", "tor": "a Tor profile of the check's own",
               "showcase-vpn": "a Tor profile of the check's own",
               **{name: "--model" for name in ("coding-letta", "coding-openhands", "coding-pi", "default-letta",
                                               "default-openhands", "default-pi")}}


# --- the check and every preset's plan -------------------------------------------------------------------------------

def test_presets_live_is_a_check_of_every_built_in_preset():
    check = LG.BY_NAME["presets-live"]
    assert check.optional and check.needs == ("sandbox", "model", "presets") and not check.podman_only
    assert check.tests == ("tests/test_live_gaps.py::test_live_preset_smoke_run",)
    assert "presets-live" not in LG.LLM_CHECKS, "never joins a whole run (hours): --only presets-live"
    assert "presets-live" in LG.LLM_USERS
    files = sorted(p.stem for p in (ROOT / "sandbox" / "presets").glob("*.json") if not p.name.endswith(".rules.json"))
    assert sorted(PRESETS) == files and len(files) >= 90
    live = (ROOT / "tests" / "test_live_gaps.py").read_text()
    assert "@pytest.mark.parametrize(\"name\", list(PRESETS))" in live


@pytest.mark.parametrize("name", sorted(PRESETS))
def test_every_preset_has_a_plan_or_a_skip_with_its_reason_and_command(name, tmp_path):
    preset = PRESETS[name]
    plan = LG.preset_plan(preset, llm=LLM, folders=tmp_path / "v", gpu=True)
    assert plan["agent"] == (preset.get("agents") or P.AGENTS)[0]
    assert plan["by_hand"].startswith(f"sandbox-ai {plan['agent']} --preset {name}")
    if name in NEEDS_YOURS:
        assert plan["skip"] and NEEDS_YOURS[name] in plan["skip"], plan["skip"]
        assert f"sandbox-ai {plan['agent']} --preset {name}" in plan["skip"], "the command to run it yourself"
        return
    if name in LLM_REFUSED:
        assert LLM_REFUSED[name] in plan["skip"] and "--provider-login" in plan["skip"], plan["skip"]
        assert LG.preset_plan(preset, llm=LLM, provider_login=True, folders=tmp_path / "v", gpu=True)["skip"] is None
        return
    assert plan["skip"] is None, plan["skip"]
    # The toolchains it brings: added to the check's store first (preset_setup).
    assert plan["toolchains"] == [value for value, _ in P.option_values(preset, {}, "--toolchain")]
    options = plan["options"]
    assert options[:2] == ["--preset", name] and "--no-auth" in options
    assert "--llm" in options or any(option.endswith("=" + LLM) for option in options), "the check's model"
    if "--llm" not in preset.get("options", []):
        assert options[options.index("--llm") + 1] == LLM and "--no-provider-defaults" in options
    assert set(plan["backends"]) <= {"podman", "bwrap"} and "podman" in plan["backends"]
    for folder in plan["folders"]:
        assert folder.startswith(str(tmp_path / "v")), "a folder of the check's own, never one of yours"
    # without a model: skipped, saying what to give
    assert "needs a model" in LG.preset_plan(preset, folders=tmp_path / "v", gpu=True)["skip"]
    # with --provider-login: the agent's own provider (no --llm of the check's, no --no-auth)
    login = LG.preset_plan(preset, llm=LLM, provider_login=True, folders=tmp_path / "v", gpu=True)
    assert "--no-auth" not in login["options"]
    if "--llm" not in preset.get("options", []):
        assert "--llm" not in login["options"]


def test_values_of_yours_from_the_preset_values_file(tmp_path):
    path = tmp_path / "values.json"
    path.write_text(json.dumps({"pentest-lab": {"target": "10.20.30.40"}, "tor": {"tor": "anon"}}))
    given = LG.preset_values(path)
    plan = LG.preset_plan(PRESETS["pentest-lab"], given["pentest-lab"], llm=LLM, folders=tmp_path)
    assert plan["skip"] is None and "target=10.20.30.40" in plan["options"]
    for bad in ("[]", "{\"x\": [1]}", "{\"x\": {\"a\": [1]}}", "not json"):
        path.write_text(bad)
        with pytest.raises(ValueError, match="--preset-values"):
            LG.preset_values(path)


def test_a_gpu_preset_without_a_gpu_and_the_podman_only_ones(tmp_path):
    gpu = [name for name, preset in PRESETS.items() if "--gpu" in preset.get("options", [])]
    assert gpu, "data-ml and pixel-art-factory take --gpu"
    assert LG.preset_plan(PRESETS["data-ml"], llm=LLM, folders=tmp_path, gpu=False)["skip"] is None, \
        "its --gpu takes an optional value: left out, it runs without one"
    data = LG.preset_plan(PRESETS["data-ml"], {"gpu": "dri"}, llm=LLM, folders=tmp_path, gpu=False)
    assert "needs a GPU" in data["skip"] and "run it where one is: sandbox-ai claude --preset data-ml" in data["skip"]
    services = LG.preset_plan(PRESETS["database-services"], llm=LLM, folders=tmp_path)
    assert services["backends"] == ["podman"] and services["podman_only"] == ["--service"]
    assert services["services"] == ["db", "cache"] and services["parts"] == ["service db", "service cache"]
    android = LG.preset_plan(PRESETS["android-debugging"], llm=LLM, folders=tmp_path)
    assert [server for server, _ in android["mcp"]] == ["jadx", "cfr", "smali", "frida"]
    assert dict(android["mcp"])["smali"] == ["/work", "/out"], "the folders the preset mounts into each server"
    goose = LG.preset_plan(PRESETS["coding-goose"], llm=LLM, folders=tmp_path)
    assert goose["api_keys"] == ["ANTHROPIC_API_KEY"], "a made-up key of the check's own, its host closed"


# --- every planned start through the real ./sandbox-ai (a dry run: no engine here) ----------------------------------

@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """A configuration of the check's kind: its LLM server, a made-up key for the presets' --api-key, the MCP servers
    the presets name as stand-in definitions (sandbox-ai mcp add pulls images: no network here), your service images
    as user-images.json records them."""
    root = tmp_path_factory.mktemp("presets-live")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("SANDBOX_AI_", "XDG_"))}
    for name, sub in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                      ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache")):
        (root / sub).mkdir()
        env[name] = str(root / sub)
    env.update(SANDBOX_AI_SECRETS="file", SANDBOX_AI_SECRETS_PLAIN="1", SANDBOX_AI_LLM_NO_CONTEXT_FETCH="1",
               SAI_LIVE_MADE_UP="sai-lc-made-up-key-0123456789",
               # Podman's --init has its init binary on the check's host, whatever this machine has (conftest.py: an
               # MCP server's start needs it; without, every preset with one was refused where catatonit is missing)
               SANDBOX_AI_INIT_BINARY=os.environ.get("SANDBOX_AI_INIT_BINARY") or "/usr/libexec/podman/catatonit")
    done = subprocess.run([str(SAI), "llm", "add", "studio", "--kind", "lmstudio", "--url", "http://192.168.1.20:1234",
                           "--model", "qwen3.8-flash-next"], capture_output=True, text=True, env=env, timeout=120)
    assert done.returncode == 0, done.stderr
    done = subprocess.run([str(SAI), "vpn", "add", LG.TOR_PROFILE, "--tor"], capture_output=True, text=True, env=env,
                          timeout=120)
    assert done.returncode == 0, done.stderr
    keys = {key for preset in PRESETS.values() for key in LG.preset_plan(preset, llm=LLM, folders=root)["api_keys"]}
    for key in sorted(keys):
        done = subprocess.run([str(SAI), "secrets", "set", key, "--from-env", "SAI_LIVE_MADE_UP"], capture_output=True,
                              text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
    servers = {}
    for preset in PRESETS.values():
        for server, targets in LG.preset_plan(preset, llm=LLM, folders=root, gpu=True)["mcp"]:
            mounts = servers.setdefault(server, set())
            mounts.update(targets)
    definitions = {}
    for server, targets in servers.items():
        if targets:
            definitions[server] = {"image": f"ghcr.io/c0decave/sandbox-ai-mcp-{server}@sha256:" + "a" * 64,
                                   "network": "none", "description": f"stand-in for {server}",
                                   "mounts": [{"target": target, "host": str(root / "mcp" / server / target.strip("/")),
                                               "mode": "rw"} for target in sorted(targets)]}
            for target in targets:
                (root / "mcp" / server / target.strip("/")).mkdir(parents=True, exist_ok=True)
        else:
            definitions[server] = {"command": ["python3", "-c", "pass"], "options": ["--offline"],
                                   "description": f"stand-in for {server}"}
    (root / "config" / "sandbox-ai").mkdir(parents=True, exist_ok=True)
    (root / "config" / "sandbox-ai" / "mcp.json").write_text(json.dumps(definitions))
    images = {name: {"kind": "pulled", "image": f"docker.io/library/{name}@sha256:" + str(i) * 64,
                     "id": "sha256:" + str(i + 5) * 64, "digest": "sha256:" + str(i) * 64, "user": user, "ports": [],
                     "os": {}}
              for i, (name, user) in enumerate((("postgres", "999:999"), ("redis", "999:1000"),
                                                ("showcase-web", "101:101")))}
    (root / "config" / "sandbox-ai" / "user-images.json").write_text(json.dumps({"version": 1, "images": images}))
    # Podman of the check's host: the images the check has before it starts anything are there (each MCP server's,
    # added by the check; every agent's, built - a command server runs in codex's; your services'), whatever this
    # machine's Podman holds (none on a CI runner: every image server was refused as "not there"; no Podman at all:
    # nothing was asked). Only `image exists` is a dry run's question; any other call fails loudly.
    agents = {LG.preset_plan(preset, llm=LLM, folders=root, gpu=True)["agent"] for preset in PRESETS.values()}
    there = sorted({definition["image"] for definition in definitions.values() if "image" in definition}
                   | {f"localhost/{agent}-sandbox:latest" for agent in agents | {"codex"}}
                   | {entry["image"] for entry in images.values()})
    (root / "podman-images.json").write_text(json.dumps(there))
    fake = root / "fake-bin"
    fake.mkdir()
    (fake / "podman").write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "if args[:2] == ['image', 'exists'] and len(args) == 3:\n"
        f"    sys.exit(0 if args[2] in json.load(open({str(root / 'podman-images.json')!r})) else 1)\n"
        "print('stand-in podman of presets-live: not a dry run\\'s call: ' + ' '.join(args), file=sys.stderr)\n"
        "sys.exit(125)\n")
    (fake / "podman").chmod(0o755)
    env["PATH"] = f"{fake}{os.pathsep}{env.get('PATH', '/usr/bin:/bin')}"
    # The toolchains the language presets bring, as preset_setup adds them (here: stand-ins, from a catalog nobody
    # signed - the check adds the signed catalog's).
    catalog = TF.catalog(root / "toolchain-catalog")
    for name in sorted({name for preset in PRESETS.values()
                        for name in LG.preset_plan(preset, llm=LLM, folders=root, gpu=True)["toolchains"]}):
        done = subprocess.run([str(SAI), "toolchain", "add", name, "--catalog", catalog, "--unsigned"],
                              capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr
    yield root, env
    TF.writable(root)


def dry_run(root, env, plan, backend):
    for folder in plan["folders"]:
        Path(folder).mkdir(parents=True, exist_ok=True)
    work = root / "w"
    work.mkdir(exist_ok=True)
    command = [str(SAI), plan["agent"], "--backend", backend, "--workspace", str(work), *plan["options"],
               "--dangerously-skip-permissions", "--dry-run", "--", *LG.one_shot(plan["agent"], "say LIVE")]
    return subprocess.run(command, capture_output=True, text=True, env=env, timeout=180, cwd=work)


@pytest.mark.parametrize("name", sorted(set(PRESETS) - set(NEEDS_YOURS)))
def test_every_planned_start_dry_runs_through_sandbox_ai(host, name):
    """The start presets-live makes of each preset, as the launcher reads it: refused by nothing with Podman and with
    each other backend the plan says it can use (the live check then runs exactly this, without --dry-run); those a
    start with --llm refuses by design, as they run with --provider-login."""
    root, env = host
    plan = LG.preset_plan(PRESETS[name], llm=LLM, provider_login=name in LLM_REFUSED, folders=root / "v" / name,
                          gpu=True)
    assert plan["skip"] is None
    for backend in plan["backends"]:
        done = dry_run(root, env, plan, backend)
        assert done.returncode == 0, f"{name} ({backend}): {done.stderr[-2500:]}"
        found = json.loads(done.stdout)
        assert found["agent"] == plan["agent"]
        assert [entry["name"] for entry in found["toolchains"]] == TF.with_required(plan["toolchains"])
        if "--llm" in plan["options"]:
            assert found.get("llm"), f"{name}: the check's model is its model"


# --- the live test with a stand-in launcher --------------------------------------------------------------------------

FAKE_LAUNCHER = r'''#!/usr/bin/env python3
"""A stand-in for ./sandbox-ai in presets-live's unit test: the agent "writes" live.txt and replies with the nonce;
it says each MCP server and service of the preset started, unless SAI_FAKE_QUIET names it."""
import json, os, re, sys
from pathlib import Path
argv = sys.argv[1:]
work = Path(argv[argv.index("--workspace") + 1])
preset = json.loads(Path(os.environ["SAI_FAKE_PRESETS"], argv[argv.index("--preset") + 1] + ".json").read_text())
prompt = argv[-1]
if "--dry-run" in argv:
    if os.environ.get("SAI_FAKE_REFUSE"):
        print("agent-sandbox: " + os.environ["SAI_FAKE_REFUSE"], file=sys.stderr)
        sys.exit(1)
    print(json.dumps({"agent": argv[0]}))
    sys.exit(0)
if os.environ.get("SAI_FAKE_FAIL"):
    print("agent-sandbox: the sandbox did not start: " + os.environ["SAI_FAKE_FAIL"], file=sys.stderr)
    sys.exit(3)
for server in preset.get("mcp") or []:
    name = server.split(":")[-1]
    if name != os.environ.get("SAI_FAKE_QUIET"):
        print(f"agent-sandbox: MCP server {name} started (container x)", file=sys.stderr)
nonce = re.search(r"LIVE[0-9A-F]{8}", prompt)
if nonce:
    (work / "live.txt").write_text(nonce.group(0) + "\n")
    print(nonce.group(0))
else:
    print((work / "nonce.txt").read_text().strip())
'''


@pytest.fixture
def live(tmp_path, monkeypatch):
    """tests/test_live_gaps.py as a module, its launcher a stand-in (FAKE_LAUNCHER), its LLM server and images
    stand-ins too, SAI_LIVE_PRESET_DIR a folder of this test's."""
    module = load("live_gaps_tests_for_presets", ROOT / "tests" / "test_live_gaps.py")
    fake = tmp_path / "sandbox-ai"
    fake.write_text(FAKE_LAUNCHER)
    fake.chmod(0o755)
    monkeypatch.setattr(module, "SAI", fake)
    calls = []

    def sai(env, *args, timeout=180, input=None):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(module, "sai", sai)
    monkeypatch.setattr(module, "harness_image", lambda agent, env: f"localhost/{agent}-sandbox:latest")
    monkeypatch.setattr(module, "llm_server", lambda tmp, model: (
        {"name": "studio"}, {**module.environment(tmp), "SAI_FAKE_PRESETS": str(ROOT / "sandbox" / "presets")}))
    facts = tmp_path / "facts"
    facts.mkdir()
    monkeypatch.setenv("SAI_LIVE_PRESET_DIR", str(facts))
    monkeypatch.setenv("SAI_LIVE_LLM_ENDPOINT", str(tmp_path / "endpoint.json"))
    for name in ("SAI_LIVE_PRESET_VALUES", "SAI_LIVE_PRESETS", "SAI_LIVE_PROVIDER_LOGIN", "SAI_FAKE_FAIL",
                 "SAI_FAKE_QUIET", "SAI_FAKE_REFUSE"):
        monkeypatch.delenv(name, raising=False)
    return SimpleNamespace(module=module, facts=facts, calls=calls)


def found(live, name, backend="podman"):
    return json.loads((live.facts / f"{name}.{backend}.json").read_text())


def run(live, request, tmp_path, name, backend="podman"):
    live.module.test_live_preset_smoke_run(request, backend, tmp_path / name, name, None)


def test_the_live_run_of_a_preset_with_its_mcp_servers(live, request, tmp_path, monkeypatch):
    (tmp_path / "android-debugging").mkdir()
    run(live, request, tmp_path, "android-debugging")
    record = found(live, "android-debugging")
    assert record["outcome"] == "passed" and record["agent"] == "claude"
    assert record["parts"] == [["MCP server jadx", True], ["MCP server cfr", True], ["MCP server smali", True],
                               ["MCP server frida", True]]
    added = [call for call in live.calls if call[:2] == ("mcp", "add")]
    assert [call[2] for call in added] == ["mcp:jadx", "mcp:cfr", "mcp:smali", "mcp:frida"], \
        "from the catalog, by the preset's names"
    assert added[2][3:] == ("--mount", f"/work={tmp_path}/android-debugging/mcp/smali/work:rw", "--mount",
                            f"/out={tmp_path}/android-debugging/mcp/smali/out:rw")
    # a server that does not come up: the preset failed, with which one
    monkeypatch.setenv("SAI_FAKE_QUIET", "cfr")
    (tmp_path / "again").mkdir()
    with pytest.raises(pytest.fail.Exception, match="did not come up: MCP server cfr"):
        live.module.test_live_preset_smoke_run(request, "podman", tmp_path / "again", "android-debugging", None)
    assert found(live, "android-debugging")["outcome"] == "failed"


def test_the_live_run_adds_a_server_from_the_catalog_the_preset_names(live, request, tmp_path):
    """firmware-iot names mcp:binwalk, which axross-mcp-hub has too: `mcp add binwalk` was refused there ("in several
    catalogs"), and the preset failed before it started. The check adds the entry the preset names."""
    (tmp_path / "firmware-iot").mkdir()
    run(live, request, tmp_path, "firmware-iot")
    assert found(live, "firmware-iot")["outcome"] == "passed"
    assert [call[2] for call in live.calls if call[:2] == ("mcp", "add")] == ["mcp:binwalk"]


@pytest.mark.parametrize("own", [False, True], ids=["signed-catalog", "own-builds"])
def test_the_live_run_of_a_language_preset_adds_its_toolchain_first(live, request, tmp_path, monkeypatch, own):
    """rust brings its toolchain (--toolchain rust): added to the run's own store before the start (from the signed
    catalog, or SAI_LIVE_TOOLCHAINS's own builds, unsigned); one that cannot be added fails the preset with the
    command and its last line."""
    if own:
        monkeypatch.setenv("SAI_LIVE_TOOLCHAINS", str(tmp_path / "toolchains.json"))
    else:
        monkeypatch.delenv("SAI_LIVE_TOOLCHAINS", raising=False)
    (tmp_path / "rust").mkdir()
    run(live, request, tmp_path, "rust")
    assert found(live, "rust")["outcome"] == "passed"
    added = [call for call in live.calls if call[:2] == ("toolchain", "add")]
    assert added == [("toolchain", "add", "rust", *(["--catalog", str(tmp_path / "toolchains.json"), "--unsigned"]
                                                     if own else []))]
    monkeypatch.setattr(live.module, "sai", lambda env, *args, **kw: subprocess.CompletedProcess(
        args, 1, "", "sandbox-ai toolchain: no toolchain rust for aarch64\n") if args[0] == "toolchain" else
        subprocess.CompletedProcess(args, 0, "", ""))
    (tmp_path / "again").mkdir()
    with pytest.raises(pytest.fail.Exception):
        live.module.test_live_preset_smoke_run(request, "podman", tmp_path / "again", "rust", None)
    assert found(live, "rust")["why"] == "sandbox-ai toolchain add rust: sandbox-ai toolchain: no toolchain rust for aarch64"


def test_the_live_run_of_a_read_only_preset_reads_the_nonce(live, request, tmp_path):
    (tmp_path / "time-capsule").mkdir()
    run(live, request, tmp_path, "time-capsule")
    assert found(live, "time-capsule")["outcome"] == "passed"


def test_a_preset_that_needs_a_value_of_yours_is_skipped_with_the_command(live, request, tmp_path):
    (tmp_path / "pentest-lab").mkdir()
    with pytest.raises(pytest.skip.Exception, match="needs a value of yours: target"):
        run(live, request, tmp_path, "pentest-lab")
    record = found(live, "pentest-lab")
    assert record["outcome"] == "skipped" and "--preset-value target=HOST" in record["why"]


def test_a_preset_whose_start_fails_says_its_last_line(live, request, tmp_path, monkeypatch):
    monkeypatch.setenv("SAI_FAKE_FAIL", "pasta: no such device")
    (tmp_path / "coding-claude").mkdir()
    with pytest.raises(pytest.fail.Exception, match="exit 3"):
        run(live, request, tmp_path, "coding-claude")
    assert found(live, "coding-claude")["why"] == "agent-sandbox: the sandbox did not start: pasta: no such device"


def test_a_start_the_launcher_refuses_on_this_host_is_skipped_with_its_line(live, request, tmp_path, monkeypatch):
    """Something of yours the start needs and this host lacks (your service image, say): the dry run first says it,
    and the preset is skipped with that line and the command - never half started."""
    monkeypatch.setenv("SAI_FAKE_REFUSE", "--service db: no image postgres of yours; add it with: sandbox-ai images "
                                          "pull docker.io/library/postgres --as postgres")
    (tmp_path / "database-services").mkdir()
    with pytest.raises(pytest.skip.Exception, match="the launcher refuses this start here"):
        run(live, request, tmp_path, "database-services")
    record = found(live, "database-services")
    assert record["outcome"] == "skipped" and "no image postgres of yours" in record["why"]
    assert "sandbox-ai claude --preset database-services" in record["why"]


def test_a_podman_only_preset_under_bubblewrap(live, request, tmp_path):
    (tmp_path / "database-services").mkdir()
    with pytest.raises(pytest.skip.Exception, match="Podman only: --service"):
        run(live, request, tmp_path, "database-services", "bwrap")


def test_the_preset_table_of_the_report(tmp_path):
    for name, backend, record in (
            ("android-debugging", "podman", {"outcome": "passed", "seconds": 61.0, "agent": "claude",
                                             "parts": [["MCP server jadx", True], ["MCP server cfr", True]]}),
            ("android-debugging", "bwrap", {"outcome": "failed", "agent": "claude", "why": "started, but did not come "
                                            "up: MCP server cfr", "parts": [["MCP server jadx", True],
                                                                             ["MCP server cfr", False]]}),
            ("pentest-lab", "podman", {"outcome": "skipped", "agent": "claude", "why": "needs a value of yours"}),
            ("coding-claude", "podman", {"outcome": "passed", "seconds": 30, "agent": "claude", "parts": []})):
        (tmp_path / f"{name}.{backend}.json").write_text(json.dumps({"preset": name, "backend": backend, **record}))
    rows = {row["preset"]: row for row in LG.preset_rows(tmp_path)}
    assert rows["android-debugging"]["podman"] == "started (61 s)"
    assert rows["android-debugging"]["bwrap"] == "failed: started, but did not come up: MCP server cfr"
    assert rows["android-debugging"]["part"] == "came up: MCP server jadx, MCP server cfr"
    assert rows["pentest-lab"] == {"preset": "pentest-lab", "agent": "claude", "podman": "skipped: needs a value of "
                                   "yours", "bwrap": "not run", "part": "-", "by_hand": ""}
    assert rows["coding-claude"]["part"] == "-"
    report = {"version": 1, "date": "X", "facts": {}, "prerequisites": [], "network": [], "counts": {},
              "checks": [{"name": "presets-live", "proves": "p", "outcome": "failed", "seconds": 1, "missing": [],
                          "tests": [], "tail": [], "presets": list(rows.values())}]}
    text = LG.markdown(report)
    assert "| preset | agent | podman | bwrap | its part |" in text
    assert "| android-debugging | claude | started (61 s) | failed: started, but did not come up: MCP server cfr |" \
        in text


def test_prepare_presets_and_the_model(tmp_path):
    def given(**more):
        return SimpleNamespace(**{"preset_values": None, "presets": None, "provider_login": False, "yes": False,
                                  **more})
    env = {}
    assert LG.prepare_presets(given(presets="nope,coding-claude"), tmp_path, env) == \
        "--presets: no built-in preset nope"
    path = tmp_path / "v.json"
    path.write_text(json.dumps({"nope": {}}))
    assert "no built-in preset nope" in LG.prepare_presets(given(preset_values=path), tmp_path, env)
    path.write_text(json.dumps({"tor": {"tor": "anon"}}))
    assert LG.prepare_presets(given(preset_values=path, presets="tor"), tmp_path, env) is None
    assert env["SAI_LIVE_PRESET_VALUES"] == str(path.resolve()) and env["SAI_LIVE_PRESETS"] == "tor"
    login = given(provider_login=True)
    assert LG.prepare_presets(login, tmp_path, env, io.StringIO("")) is None and login.provider_login is False
    assert "SAI_LIVE_PROVIDER_LOGIN" not in env, "not confirmed (no terminal, no --yes): your logins stay unread"
    login = given(provider_login=True, yes=True)
    assert LG.prepare_presets(login, tmp_path, env) is None and env["SAI_LIVE_PROVIDER_LOGIN"] == "1"
    assert LG.model_found({"llm": LG.Found(False, "give --llm NAME")}, login).ok
    assert not LG.model_found({"llm": LG.Found(False, "give --llm NAME")}, given()).ok
    assert LG.model_found({"llm": LG.Found(True, "server mac")}, given()).detail == "server mac"


# --- the importer and the presets' live light ------------------------------------------------------------------------

def report(date="20261123-091500", version="0.1.212", podman="started (61 s)", bwrap="failed: pasta: x"):
    return {"version": 1, "date": date, "facts": {"sandbox-ai": f"sandbox-ai {version}", "commit": "abc1234",
                                                   "date": date},
            "checks": [{"name": "presets-live", "presets": [
                {"preset": "coding-claude", "agent": "claude", "podman": podman, "bwrap": bwrap, "part": "-"},
                {"preset": "android-debugging", "agent": "claude", "podman": "started (90 s)", "bwrap": "not run",
                 "part": "came up: MCP server jadx"},
                {"preset": "pentest-lab", "agent": "claude", "podman": "skipped: needs a value of yours",
                 "bwrap": "skipped: needs a value of yours", "part": "-"}]},
                {"name": "harnesses-live", "harnesses": [
                    {"agent": "goose", "image": "there before", "podman": "passed (40 s)", "bwrap": "not run"}]}]}


def test_the_importer_takes_what_passed_or_failed_and_nothing_else(tmp_path):
    entries = IMPORT.entries_of(report())
    keys = {(e["kind"], e["name"], e["backend"]): e for e in entries}
    assert set(keys) == {("preset", "coding-claude", "podman"), ("preset", "coding-claude", "bwrap"),
                         ("preset", "android-debugging", "podman"), ("harness", "goose", "podman")}
    assert keys[("preset", "coding-claude", "bwrap")]["result"] == "failed"
    assert keys[("preset", "coding-claude", "bwrap")]["detail"] == "pasta: x"
    assert keys[("preset", "android-debugging", "podman")]["detail"] == "90 s, came up: MCP server jadx"
    assert all(e["date"] == "2026-11-23" and e["version"] == "0.1.212" and
               e["report"] == "REPORT-20261123-091500 (commit abc1234)" for e in entries)
    evidence = tmp_path / "live_evidence.json"
    path = tmp_path / "REPORT.json"
    path.write_text(json.dumps(report()))
    lines = []
    assert IMPORT.main([str(path), "--evidence", str(evidence)], out=lines.append) == 0
    assert "added: preset coding-claude (bwrap): failed - pasta: x" in lines
    # a newer report replaces; an older one does not
    path.write_text(json.dumps(report(date="20261124-080000", version="0.1.213", bwrap="started (20 s)")))
    assert IMPORT.main([str(path), "--evidence", str(evidence)], out=lines.append) == 0
    path.write_text(json.dumps(report(date="20261101-080000", version="0.1.200", podman="failed: old")))
    assert IMPORT.main([str(path), "--evidence", str(evidence)], out=lines.append) == 0
    document = IMPORT.load(evidence)
    claude = {e["backend"]: e for e in document["entries"] if e["name"] == "coding-claude"}
    assert claude["bwrap"]["result"] == "passed" and claude["podman"]["version"] == "0.1.213"
    assert IMPORT.main(["--check", "--evidence", str(evidence)], out=lines.append) == 0
    with pytest.raises(ValueError, match="no date"):
        IMPORT.entries_of({"facts": {}, "checks": []})


@pytest.mark.parametrize("change, message", [
    (lambda e: e.update(result="skipped"), "result passed/failed"),
    (lambda e: e.update(date="23.11.2026"), "date YYYY-MM-DD"),
    (lambda e: e.update(version="new"), "version"),
    (lambda e: e.update(report=""), "report names"),
    (lambda e: e.update(extra=1), "an object of"),
])
def test_the_evidence_file_is_checked(change, message):
    entry = {"kind": "preset", "name": "coding-claude", "backend": "podman", "agent": "claude", "result": "passed",
             "date": "2026-11-23", "version": "0.1.212", "report": "REPORT-1", "detail": ""}
    change(entry)
    with pytest.raises(ValueError, match=message):
        IMPORT.check({"schema": 1, "entries": [entry]})


def test_the_shipped_evidence_file_is_valid_and_names_only_what_exists():
    document = IMPORT.load()
    harnesses = set(LG.HARNESSES)
    for entry in document["entries"]:
        assert entry["name"] in (PRESETS if entry["kind"] == "preset" else harnesses), entry
    done = subprocess.run([sys.executable, str(ROOT / "scripts" / "import_live_report.py"), "--check"],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_preset_shows_its_live_light_only_with_evidence(tmp_path, monkeypatch, capsys):
    evidence = tmp_path / "live_evidence.json"
    document, _ = IMPORT.merged(IMPORT.empty(), IMPORT.entries_of(report()))
    evidence.write_text(json.dumps(document))
    monkeypatch.setattr(P, "LIVE_EVIDENCE", evidence)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    catalog = P.catalog()
    green = P.live_light(catalog["android-debugging"])
    assert green["light"] == "green" and green["date"] == "2026-11-23" and green["version"] == "0.1.212"
    assert P.live_text(green) == "✓ live check passed (podman) on 2026-11-23, sandbox-ai 0.1.212"
    red = P.live_light(catalog["coding-claude"])
    assert red["light"] == "red" and red["why"] == "bwrap: pasta: x"
    assert P.live_text(red) == "✗ live check failed on 2026-11-23, sandbox-ai 0.1.212 (bwrap: pasta: x)"
    assert P.live_light(catalog["pentest-lab"]) is None, "skipped: no evidence, no light"
    assert P.live_light({**catalog["coding-claude"], "_origin": "yours"}) is None, "only the built-in ones"
    lines = []
    assert P.main(["list"], out=lines.append) == 0
    listed = {line.split()[0]: line for line in lines if line.startswith("  ")}
    assert listed["android-debugging"].endswith("✓ live 2026-11-23")
    assert listed["coding-claude"].endswith("✗ live 2026-11-23")
    assert not any(mark in listed["pentest-lab"] for mark in ("✓", "✗", "live")), "no data: nothing extra"
    lines.clear()
    assert P.main(["show", "coding-claude"], out=lines.append) == 0
    assert "  live: ✗ live check failed on 2026-11-23, sandbox-ai 0.1.212 (bwrap: pasta: x)" in lines
    assert "    podman: passed on 2026-11-23 (sandbox-ai 0.1.212, REPORT-20261123-091500 (commit abc1234)): 61 s" \
        in lines
    lines.clear()
    assert P.main(["show", "pentest-lab"], out=lines.append) == 0
    assert not any("live" in line for line in lines)
    lines.clear()
    assert P.main(["show", "android-debugging", "--json"], out=lines.append) == 0
    assert json.loads(lines[0])["live"]["light"] == "green"
    evidence.write_text("{broken")
    assert P.live_light(P.catalog()["coding-claude"]) is None, "a damaged file: no light, nothing refused"


def test_the_shipped_presets_say_nothing_extra_without_evidence():
    """No data yet (an empty sandbox/live_evidence.json): `presets list` has no live mark on any line, `show --json`
    says live null - grey, no negative word."""
    if IMPORT.load()["entries"]:
        pytest.skip("the shipped evidence has entries")
    done = subprocess.run([str(SAI), "presets", "list"], capture_output=True, text=True, timeout=120,
                          env={**os.environ, "HOME": "/nonexistent-home", "XDG_CONFIG_HOME": "/nonexistent-config"})
    assert done.returncode == 0 and " live " not in done.stdout


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{}</testsuite></testsuites>"""


def test_the_whole_way_from_the_live_check_to_the_presets_light(tmp_path, monkeypatch):
    """scripts/live_check.sh --only presets-live --llm mac --yes (main, as the entry point runs it) on a host whose
    Podman is a stand-in and whose pytest run is faked to write what test_live_preset_smoke_run writes: the report has
    the table per preset, scripts/import_live_report.py makes it live_evidence.json, and the presets show it."""
    home = tmp_path / "home" / "alice"
    (home / ".config" / "sandbox-ai").mkdir(parents=True)
    (home / ".config" / "sandbox-ai" / "llm.json").write_text(json.dumps({"version": 1, "endpoints": {
        "mac": {"kind": "lmstudio", "url": "http://192.168.1.20:1234"}}}))
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "podman").write_text(
        "#!/bin/sh\ncase \"$1 $2\" in\n"
        "  'info --format') echo '{\"version\": {\"Version\": \"5.6.1\"}, \"host\": {\"cgroupVersion\": \"v2\", "
        "\"security\": {\"rootless\": true}}}' ;;\n  'image exists') exit 0 ;;\nesac\n")
    (fake / "unshare").write_text("#!/bin/sh\nexit 0\n")
    for name in ("podman", "unshare"):
        (fake / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(LG.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(LG, "subordinate_ids", lambda *a, **k: LG.Found(True, "ranges"))
    real = LG.load_live_check()
    monkeypatch.setattr(real, "inside_a_container", lambda: False)
    monkeypatch.setattr(real, "pytest_python", lambda directory: sys.executable)
    monkeypatch.setattr(LG, "load_live_check", lambda: real)
    seen = {}

    def fake_pytest(command, log, timeout, env, cwd):
        junit = Path(next(part for part in command if part.startswith("--junitxml="))[len("--junitxml="):])
        seen.update(command=command, env=env)
        facts = Path(env["SAI_LIVE_PRESET_DIR"])
        for name, backend, record in (
                ("coding-claude", "podman", {"outcome": "passed", "seconds": 33, "agent": "claude", "parts": []}),
                ("coding-claude", "bwrap", {"outcome": "failed", "agent": "claude", "why": "bwrap: setting up uid map:"
                                            " Permission denied", "parts": []}),
                ("pentest-lab", "podman", {"outcome": "skipped", "agent": "claude", "why": "needs a value of yours"})):
            (facts / f"{name}.{backend}.json").write_text(json.dumps({"preset": name, "backend": backend, **record}))
        junit.write_text(JUNIT.format('<testcase classname="tests.test_live_gaps" '
                                      'name="test_live_preset_smoke_run[podman-coding-claude]" time="33"/>'))
        Path(log).write_text("1 passed\n")
        return 0, 40.0
    monkeypatch.setattr(LG, "run_logged", fake_pytest)
    values = tmp_path / "values.json"
    values.write_text(json.dumps({"pentest-lab": {"target": "10.1.2.3"}}))
    lines = []
    status = LG.main(["--only", "presets-live", "--llm", "mac", "--yes", "--preset-values", str(values),
                      "--out", str(tmp_path / "out"), "--work-dir", str(tmp_path)], out=lines.append,
                     stdin=io.StringIO(""))
    assert status == 0, lines
    assert seen["env"]["SAI_LIVE_PRESET_VALUES"] == str(values.resolve())
    assert "tests/test_live_gaps.py::test_live_preset_smoke_run" in seen["command"]
    path = next((tmp_path / "out").glob("REPORT-*.json"))
    report_ = json.loads(path.read_text())
    check = next(c for c in report_["checks"] if c["name"] == "presets-live")
    rows = {row["preset"]: row for row in check["presets"]}
    assert rows["coding-claude"]["podman"] == "started (33 s)"
    assert rows["coding-claude"]["bwrap"].startswith("failed: bwrap: setting up uid map")
    assert rows["pentest-lab"]["podman"] == "skipped: needs a value of yours"
    text = next((tmp_path / "out").glob("REPORT-*.md")).read_text()
    assert "| preset | agent | podman | bwrap | its part |" in text and "| coding-claude | claude | started" in text
    evidence = tmp_path / "live_evidence.json"
    said = []
    assert IMPORT.main([str(path), "--evidence", str(evidence)], out=said.append) == 0
    entries = IMPORT.load(evidence)["entries"]
    assert {(e["name"], e["backend"], e["result"]) for e in entries} == {("coding-claude", "podman", "passed"),
                                                                         ("coding-claude", "bwrap", "failed")}
    version = LG.run_command([str(SAI), "--version"])[1].split()[-1]
    assert all(e["version"] == version and e["report"].startswith("REPORT-") for e in entries)
    monkeypatch.setattr(P, "LIVE_EVIDENCE", evidence)
    light = P.live_light({**PRESETS["coding-claude"], "_origin": "built-in"})
    assert light["light"] == "red" and light["why"].startswith("bwrap: bwrap: setting up uid map")


@pytest.mark.skipif(not __import__("shutil").which("node"), reason="needs node")
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_web_page_shows_a_presets_live_light(tmp_path, language):
    """The web page's Presets table and detail: presetLive() of `presets list --json`'s "live" (app.js in node)."""
    from test_web import FAKE_DOM
    driver = tmp_path / "live.js"
    driver.write_text(FAKE_DOM + r"""
if (process.argv[3] === "de") vm.runInContext('setLanguage("de")', context);
const found = vm.runInContext(`(() => {
  const a = presetLive({ live: { light: "green", date: "2026-11-23", version: "0.1.212" } });
  const b = presetLive({ live: { light: "red", date: "2026-11-23", version: "0.1.212", why: "bwrap: x" } });
  return [a.textContent, a.className, b.textContent, b.className, presetLive({}), presetLive({ live: null }),
          presetLive({ live: { light: "grey" } })];
})()`, context);
console.log(JSON.stringify(found));
""")
    done = subprocess.run(["node", str(driver), str(ROOT / "sandbox/web/app.js"), language], capture_output=True,
                          text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    green, green_class, red, red_class, *none = json.loads(done.stdout)
    if language == "en":
        assert green == "✓ live check passed on 2026-11-23, sandbox-ai 0.1.212" and green_class == "status-ok"
        assert red == "✗ live check failed on 2026-11-23, sandbox-ai 0.1.212: bwrap: x" and red_class == "status-bad"
    else:
        assert green == "✓ Live-Prüfung bestanden am 2026-11-23, sandbox-ai 0.1.212"
        assert red == "✗ Live-Prüfung fehlgeschlagen am 2026-11-23, sandbox-ai 0.1.212: bwrap: x"
    assert none == ["", "", ""], "no data yet: nothing shown"
