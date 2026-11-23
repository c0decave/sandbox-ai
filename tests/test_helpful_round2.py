"""Helpful, not blocking - round 2: the dead ends round 1 left (docs/HELPFUL_FIXES.md "Left for round 2"), each now
offered through the one fix plan (sandbox/fixes.py) - end to end through ./sandbox-ai with only Podman faked
(tests/live_fakes.py), and the pieces on their own. User, 2026-11-22: "if something is missing or not working,
offer to help and get it run ... dont block sorry not available/integrated - build it and attach it."
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import home_guard  # noqa: E402

home_guard.ensure()

import live_fakes as F  # noqa: E402
from test_fixes_e2e import CLAUDE, QUESTION, flat, login, on_terminal  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "sandbox"


def load(name):
    spec = importlib.util.spec_from_file_location(f"helpful2_{name}", SANDBOX / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fixes = load("fixes")


@pytest.fixture
def host(tmp_path):
    made = F.Host(tmp_path / "h")
    made.add_image(CLAUDE)
    login(made)
    return made


def profiles(host):
    folder = host.home / ".config/sandbox-ai/vpn"
    return sorted(path.stem for path in folder.glob("*.json")) if folder.is_dir() else []


# --- the plan's new pieces -----------------------------------------------------------------------------------------

def test_a_refusal_that_names_its_secret_carries_the_fix_that_keeps_it():
    for message in ("--service db: no secret DB_PW; keep one with: sandbox-ai secrets set DB_PW",
                    "--publish npm: no secret NPM_TOKEN; keep one with: sandbox-ai secrets set NPM_TOKEN",
                    "LLM endpoint lm: no secret K for the header X-Key; keep it with: sandbox-ai secrets set K",
                    "VPN profile x: the secret K (the auth key) is missing; add the profile again (sandbox-ai vpn add "
                    "...) or keep it with: sandbox-ai secrets set K"):
        found = fixes.recognised(message)
        assert len(found) == 1 and found[0]["commands"] == [["sandbox-ai", "secrets", "set",
                                                             found[0]["name"]]], message
        assert found[0]["terminal"] and found[0]["verdict"] == "offered" and found[0]["needed"]
    # a hint that only mentions the command (an MCP variable that must not be a plain value) is no missing secret
    assert fixes.recognised("--mcp-env x FOO: a token, and a secret is never kept as a plain value: keep it with "
                            "sandbox-ai secrets set FOO and give --mcp-env x:FOO@FOO") == []


def two_ways():
    return [fixes.fix("vpn", "anon", "the VPN profile anon is not on this host", "make anon a Tor profile",
                      [["sandbox-ai", "vpn", "add", "anon", "--tor"]], security="through Tor", choice="vpn:anon"),
            fixes.fix("vpn", "anon", "the VPN profile anon is not on this host", "make it with its keys",
                      [["sandbox-ai", "vpn", "add", "anon", "--wireguard", "FILE"]], choice="vpn:anon",
                      ident="vpn:anon:yours", manual="keys only you have")]


def test_a_way_only_you_choose_is_asked_on_its_own_with_no_as_its_default():
    asked, said = [], []

    def read(prompt):
        asked.append(prompt)
        return ""  # (Enter: No for a way only you choose)
    with pytest.raises(ValueError) as refused:
        fixes.resolve("no VPN profile anon", two_ways(), terminal=True, read=read, out=said.append,
                      run_command=lambda *_: pytest.fail("nothing runs"))
    assert len(asked) == 1 and asked[0].endswith("make anon a Tor profile; SECURITY: through Tor: sandbox-ai vpn add "
                                                 "anon --tor? [y/N] ")
    assert "sandbox-ai vpn add anon --wireguard FILE" in str(refused.value)
    ran = []
    done = fixes.resolve("no VPN profile anon", two_ways(), terminal=True, read=lambda _p: "y", out=said.append,
                         run_command=lambda command, terminal: ran.append(command) or (0, []))
    assert done == ["vpn:anon"] and ran == [["sandbox-ai", "vpn", "add", "anon", "--tor"]]  # (the other way: not needed)


def test_a_way_only_you_choose_is_never_taken_by_fix_and_the_block_holds_only_what_you_type():
    with pytest.raises(ValueError) as refused:
        fixes.resolve("no VPN profile anon", two_ways(), fix_all=True, terminal=False,
                      run_command=lambda *_: pytest.fail("--fix takes no choice"))
    text = str(refused.value)
    block = text.split("then start again:\n")[1].split("\n")
    assert block[0] == "  sandbox-ai vpn add anon --wireguard FILE" and not block[1].startswith("  ")
    assert "or, if you want it so, make anon a Tor profile" in text and "(--fix-only vpn:anon does it)" in text
    assert "Or start again with --fix" not in text  # (--fix would do nothing of it)
    done = fixes.resolve("no VPN profile anon", two_ways(), only=["vpn:anon"], terminal=False,
                         run_command=lambda command, terminal: (0, []))
    assert done == ["vpn:anon"]


def test_a_fix_only_a_terminal_can_do_says_so_instead_of_fix():
    text = str(pytest.raises(ValueError, fixes.resolve, "--secret-env FOO: no secret FOO", [fixes.secret_fix("FOO")],
                             terminal=False).value)
    assert "  sandbox-ai secrets set FOO" in text and "Or start again on a terminal: sandbox-ai asks there" in text
    assert "with --fix" not in text


# --- the launcher, end to end ----------------------------------------------------------------------------------------

def test_a_missing_vpn_profile_offers_a_tor_profile_on_the_command_line(host):
    host.add_image("localhost/sandbox-ai-tor:latest")
    script = host.run("claude", "--vpn", "anon")
    assert script.returncode == 1 and profiles(host) == []
    text = flat(script.stderr)
    assert "no VPN profile anon (profiles: none yet)" in text  # (as before)
    assert "sandbox-ai vpn add anon --wireguard FILE" in text and "(--fix-only vpn:anon does it)" in text
    with_fix = host.run("claude", "--vpn", "anon", "--fix")
    assert with_fix.returncode == 1 and profiles(host) == []  # (--fix never makes your VPN a Tor profile)
    status, said = on_terminal(host, "claude", "--vpn", "anon", answers=[("[y/N]", "y")])
    assert profiles(host) == ["anon"], said[-2000:]
    assert "make anon a Tor profile" in flat(said) and "SECURITY: the sandbox's connections go through Tor" in flat(said)
    assert json.loads((host.home / ".config/sandbox-ai/vpn/anon.json").read_text())["type"] == "tor"


def test_the_tor_profile_is_made_by_fix_only_in_a_script(host):
    done = host.run("claude", "--vpn", "anon", "--fix-only", "vpn:anon")
    assert profiles(host) == ["anon"], done.stderr[-2000:]


def test_a_secret_a_start_needs_is_asked_for_kept_and_the_start_goes_on(host):
    script = host.run("claude", "--secret-env", "FOO")
    assert script.returncode == 1 and "  sandbox-ai secrets set FOO" in script.stderr
    assert "Or start again on a terminal: sandbox-ai asks there, then does it." in script.stderr
    status, said = on_terminal(host, "claude", "--secret-env", "FOO",
                               answers=[(QUESTION, ""), ("(not shown)", "s3cret-value")])
    assert "there is no secret FOO" in flat(said) and "s3cret-value" not in said, said[-2000:]
    assert host.stopped_at() is not None, said[-2000:]
    listed = host.run("secrets", "list")
    assert "FOO" in listed.stdout


def test_publish_without_a_login_is_offered_named_security_relevant(host):
    plan = json.loads(host.run("claude", "--publish", "npm", "--fix-plan").stdout)
    item = next(item for item in plan["fixes"] if item["id"] == "publish:npm")
    assert item["commands"] == [["sandbox-ai", "secrets", "set", "NPM_TOKEN"],
                                ["sandbox-ai", "publish-logins", "add", "npm", "--secret", "NPM_TOKEN"]]
    assert item["verdict"] == "offered" and item["terminal"] and "publish to npm as you" in item["security"]
    refused = host.run("claude", "--publish", "npm")
    assert "--publish npm: no login" in refused.stderr and "  sandbox-ai publish-logins add npm --secret NPM_TOKEN" \
        in refused.stderr


def test_publish_with_an_identity_adds_the_identitys_login_and_takes_a_kept_secret(host):
    assert host.run("identities", "add", "work").returncode == 0
    store = subprocess.run(host.command("secrets", "set", "PYPI_TOKEN"), cwd=host.workspace, env=host.env,
                           input="pypi-abc\n", capture_output=True, text=True, timeout=120)
    assert store.returncode == 0, store.stderr
    plan = json.loads(host.run("claude", "--identity", "work", "--publish", "pypi", "--fix-plan").stdout)
    item = next(item for item in plan["fixes"] if item["id"] == "publish:pypi")
    assert item["commands"] == [["sandbox-ai", "identities", "publish", "work", "pypi", "--secret", "PYPI_TOKEN"]]
    assert item["terminal"] is False  # (kept already: nothing to type, --fix does it)
    done = host.run("claude", "--identity", "work", "--publish", "pypi", "--fix")
    assert "fix 1/1 done" in done.stderr, done.stderr[-2000:]
    assert host.stopped_at() is not None, done.stderr[-2000:]


def test_a_service_image_of_sandbox_ais_own_containerfile_is_offered_to_be_built(host):
    plan = json.loads(host.run("claude", "--service", "showcase-web:port=8080,user=101:101", "--fix-plan").stdout)
    item = next(item for item in plan["fixes"] if item["kind"] == "service")
    assert item["commands"] == [["sandbox-ai", "images", "build-image", "showcase-web", "--builtin", "showcase-web"]]
    assert item["verdict"] == "offered" and "UNCHECKED" in item["security"]
    refused = host.run("claude", "--service", "showcase-web:port=8080,user=101:101")
    assert "  sandbox-ai images build-image showcase-web --builtin showcase-web" in refused.stderr
    yours = json.loads(host.run("claude", "--service", "db:port=5432", "--fix-plan").stdout)
    mine = next(item for item in yours["fixes"] if item["kind"] == "service")
    assert mine["verdict"] == "manual" and "is yours to say" in mine["why"]


def test_a_missing_service_secret_is_in_the_plan_before_anything_runs(host, tmp_path):
    record = {"version": 1, "images": {"db": {
        "kind": "pulled", "ref": "docker.io/library/postgres:17", "image": "docker.io/library/postgres@sha256:" + "a" * 64,
        "digest": "sha256:" + "a" * 64, "id": "sha256:" + "b" * 64, "reviewed": False, "added": "2026-11-01T00:00:00Z",
        "user": "999:999", "config_user": "999", "os": {}, "ports": []}}}
    path = host.home / ".config/sandbox-ai/user-images.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    plan = host.run("claude", "--service", "db:port=5432,secret=PGPASSWORD=DBPW,user=999:999", "--fix-plan")
    found = {item["id"]: item for item in json.loads(plan.stdout)["fixes"]} if plan.returncode == 0 else {}
    assert "secret:DBPW" in found, plan.stdout + plan.stderr[-2000:]
    assert found["service:db"]["commands"] == [["sandbox-ai", "images", "pull",
                                                "docker.io/library/postgres@sha256:" + "a" * 64, "--as", "db",
                                                "--replace"]]
    assert "by the digest it was added with" in found["service:db"]["title"]


# --- the services' own init -----------------------------------------------------------------------------------------

def test_every_service_containerfile_of_sandbox_ai_brings_its_own_pid_1():
    services = load("services")
    for template in services.builtin_images():
        text = (SANDBOX / "service-images" / template / "Containerfile").read_text()
        assert template in services.OWN_INIT or any(init in text for init in ("tini", "dumb-init")), template


def test_an_own_containerfiles_service_starts_without_the_hosts_catatonit(monkeypatch):
    services = load("services")
    hostinit = services.hostinit_module()
    monkeypatch.setattr(hostinit, "init_binary", lambda *a, **k: None)  # (a host without catatonit)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return argparse.Namespace(returncode=0, stdout="", stderr="")
    launcher = argparse.Namespace(podman_command=lambda a, *rest: ["podman", *rest], local_engine=lambda a: True,
                                  load_secret_store_module=lambda: None)
    service = {"name": "web", "image": "web", "env": [], "secrets": [],
               "record": {"id": "sha256:" + "c" * 64, "image": "localhost/x", "kind": "built", "builtin": "showcase-web"}}
    a = argparse.Namespace(services=[service])
    services.prepare(a, launcher, run=run)
    assert service["init"] is False  # (no --init: nginx is its own PID 1)
    service["record"] = {**service["record"], "builtin": None}
    with pytest.raises(ValueError) as refused:
        services.prepare(a, launcher, run=run)
    assert "init" in str(refused.value)


# --- MCP: an image that is gone ------------------------------------------------------------------------------------

def test_a_gone_mcp_image_pinned_by_digest_is_pulled_again_by_that_digest():
    mcp = load("mcp")
    image = "ghcr.io/x/jadx@sha256:" + "d" * 64
    item = mcp.gone_image_fix("jadx", image, signed=True)
    assert item["verdict"] == "offered" and item["commands"] == [["podman", "pull", image]] and not item["unsigned"]
    assert "its signature was checked then" in item["title"]
    built = mcp.gone_image_fix("smali", "localhost/sandbox-ai-mcp-smali:latest")
    assert built["verdict"] == "manual" and built["commands"][1] == ["sandbox-ai", "mcp", "add", "smali"]


# Round-1 review: the pull by digest was "the very image that was added (its signature was checked then)" for every
# server - also one added unsigned or named by a project file - and offered in high and paranoid as well.

@pytest.mark.parametrize("origin", ["unsigned", "project"])
def test_a_gone_unsigned_or_project_image_is_said_unsigned(origin):
    mcp = load("mcp")
    image = "ghcr.io/x/jadx@sha256:" + "d" * 64
    item = mcp.gone_image_fix("jadx", image, signed=False, project=origin == "project")
    assert item["unsigned"] and item["verdict"] == "offered" and item["commands"] == [["podman", "pull", image]]
    assert "signature" not in item["title"] and "UNSIGNED" in fixes.what(item)
    assert ("a project file names it" if origin == "project" else "added unsigned") in item["title"]
    for mode in fixes.STRICT:  # only shown, with its command: never pulled by an offer or --fix there
        strict = mcp.gone_image_fix("jadx", image, signed=False, project=origin == "project", mode=mode)
        assert strict["verdict"] == "manual" and strict["commands"] == [["podman", "pull", image]]
        assert f"not offered in the security mode {mode}" in strict["why"] and strict["unsigned"]
        listed = "\n".join(fixes.listing(fixes.judge([strict], mode)))
        assert f"podman pull {image}" in listed and "not done for you" in listed
    assert mcp.gone_image_fix("jadx", image, signed=True, mode="paranoid")["verdict"] == "offered"


def test_a_gone_image_built_here_is_built_again_not_pulled():
    mcp = load("mcp")
    item = mcp.gone_image_fix("smali", "localhost/sandbox-ai-mcp-smali@sha256:" + "e" * 64, built=True)
    assert item["verdict"] == "manual" and ["podman", "pull"] not in [c[:2] for c in item["commands"]]


SIGNED_JADX = {"image": "ghcr.io/x/jadx@sha256:" + "d" * 64, "network": "none",
               "signed_by": "SHA256:" + "k" * 43, "catalog": "https://example.org/catalog.json"}


def gone_image_plan(host, servers, *options, project=None):
    path = host.home / ".config/sandbox-ai/mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(servers))
    if project is not None:
        (host.workspace / ".sandbox-ai.json").write_text(json.dumps({"version": 1, "agent": "claude", "mcp_servers": project}))
        trusted = host.run("wizard", "trust", "--yes", str(host.workspace / ".sandbox-ai.json"))
        assert trusted.returncode == 0, trusted.stdout[-2000:]
    done = host.run("claude", "--mcp", "jadx", *options, "--fix-plan")
    assert done.returncode == 0, done.stderr[-2000:]
    return next(item for item in json.loads(done.stdout)["fixes"] if item["id"] == "mcp-image:jadx")


def test_a_gone_image_in_the_starts_plan_says_where_it_comes_from(host):
    """End to end (./sandbox-ai, Podman faked): signed - offered as the very image; added unsigned, or a project
    file's server of that name (whatever "signed_by" it claims) - UNSIGNED, and in high only shown with the command."""
    signed = gone_image_plan(host, {"jadx": SIGNED_JADX})
    assert signed["verdict"] == "offered" and not signed["unsigned"] and "signature was checked" in signed["title"]
    unsigned = gone_image_plan(host, {"jadx": {**SIGNED_JADX, "signed_by": None}})
    assert unsigned["verdict"] == "offered" and unsigned["unsigned"] and "signature" not in unsigned["title"]
    strict = gone_image_plan(host, {"jadx": {**SIGNED_JADX, "signed_by": None}}, "--mode", "high")
    assert strict["verdict"] == "manual" and strict["commands"] == [["podman", "pull", SIGNED_JADX["image"]]]
    assert "not offered in the security mode high" in strict["why"]
    claimed = gone_image_plan(host, {"jadx": SIGNED_JADX}, project={"jadx": SIGNED_JADX})
    assert claimed["unsigned"] and "a project file names it" in claimed["title"]


# --- Bubblewrap: the agent's executable ------------------------------------------------------------------------------

def test_an_agent_bubblewrap_cannot_find_is_a_choice_podman_or_installed_here(monkeypatch):
    launcher = load("agent_sandbox")
    monkeypatch.setattr(launcher.shutil, "which", lambda name: "/usr/bin/podman" if name == "podman" else None)
    found = launcher.bwrap_agent_fixes(argparse.Namespace(probe=False), "claude")
    podman, npm = found
    assert podman["options"] == {"add": ["--backend", "podman"], "remove": ["--backend"]} and podman["choice"]
    assert npm["verdict"] == "manual" and npm["commands"] == [["npm", "install", "-g", "@anthropic-ai/claude-code"]]
    assert npm["choice"] == podman["choice"]  # (with Podman chosen, nothing is left to install)
    said = []
    done = fixes.resolve("Agent executable missing: claude", found, terminal=True, read=lambda _p: "y",
                         out=said.append, run_command=lambda *_: (0, []))
    assert done == ["option:backend-podman"]


# --- the agent's LLM server that does not answer -------------------------------------------------------------------

def test_an_llm_server_that_does_not_answer_is_said_with_what_makes_it_answer(monkeypatch):
    llm = load("llm")
    found = {"name": "lm", "url": "http://192.0.2.1:1234/v1", "models": ["m"], "model": "m"}
    monkeypatch.setattr(llm, "selection", lambda a, path=None: (found, "m"))

    def unreachable(a, endpoint):
        a._llm_unreachable = ("lm", "connection refused")
        return ["m"], {}, "remembered"
    monkeypatch.setattr(llm, "server_models", unreachable)
    item = llm.unreachable_fix(argparse.Namespace(llm="lm"))
    assert item["commands"] == [["sandbox-ai", "llm", "check", "lm"]] and item["needed"] is False
    assert item["verdict"] == "manual" and "did not answer (connection refused)" in item["missing"]
    monkeypatch.setattr(llm, "server_models", lambda a, endpoint: (["m"], {}, "server"))
    assert llm.unreachable_fix(argparse.Namespace(llm="lm")) is None


# --- doctor: each with its command in the plan ---------------------------------------------------------------------

def test_doctor_names_the_commands_of_the_id_ranges_the_user_session_and_sshfs():
    doctor = load("doctor")
    ranges = doctor.fix_of("fail", "Delegated UID/GID ranges", "/etc/subuid: 0 IDs for u",
                           doctor.subid_advice("u", None))
    assert ranges["commands"][0][:3] == ["sudo", "usermod", "--add-subuids"] and ranges["verdict"] == "manual"
    session = doctor.fix_of("warn", "systemd user session", "none", "sudo loginctl enable-linger u, install ...")
    assert session["commands"][0][:3] == ["sudo", "loginctl", "enable-linger"] and session["needed"] is False
    remote = doctor.fix_of("skip", "Remote folders (sshfs)", "sshfs is not installed: ...", "install sshfs")
    assert remote["kind"] == "tool" and remote["needed"] is False and remote["verdict"] == "manual"
    assert doctor.fix_of("skip", "Something else", "x", "y") is None


def test_a_remote_folder_without_sshfs_is_refused_with_the_package_command(host):
    done = host.run("claude", "--workspace", "sshfs://u@example.org/srv/x")
    assert done.returncode == 1 and "needs sshfs on this host" in done.stderr
    assert "What fixes it (copy these lines)" in done.stderr and "sshfs" in done.stderr.split("What fixes it")[1]


# --- a device your user cannot open --------------------------------------------------------------------------------

def test_a_device_your_user_cannot_open_gets_its_udev_rule_as_commands(monkeypatch):
    launcher = load("agent_sandbox")
    monkeypatch.setattr(launcher, "udev_hint", lambda path: 'SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", TAG+="uaccess"')
    item = launcher.udev_fix("/dev/ttyUSB0")
    assert item["verdict"] == "manual" and not item["needed"] and item["commands"][0][:3] == ["sudo", "sh", "-c"]
    assert launcher.UDEV_RULES in item["commands"][0][3] and item["commands"][1:] == [
        ["sudo", "udevadm", "control", "--reload"], ["sudo", "udevadm", "trigger"]]
    monkeypatch.setattr(launcher, "udev_hint", lambda path: None)
    assert launcher.udev_fix("/dev/ttyUSB0") is None


# --- resume of a kept session ----------------------------------------------------------------------------------------

def test_resume_asks_about_what_the_sessions_start_lacks(monkeypatch):
    sessions = load("sessions")
    plan = {"fixes": [fixes.fix("image", "claude", "the image of claude is not built", "build it",
                                [["sandbox-ai", "build", "claude"]], duration="~5 min"),
                      fixes.fix("option", "with-claude", "x", "y", options={"add": ["--with", "claude"]})],
            "questions": "all", "mode": "medium"}
    monkeypatch.setattr(sessions, "plan_of", lambda name, data: plan)
    ran = []
    done = sessions.fix_first("s1", {"agent": "claude"}, terminal=True, read=lambda _p: "",
                              run_command=lambda command, terminal: ran.append(command) or (0, []))
    assert done == ["image:claude"] and ran == [["sandbox-ai", "build", "claude"]]
    with pytest.raises(ValueError) as refused:
        sessions.fix_first("s1", {"agent": "claude"}, terminal=True, read=lambda _p: "n")
    assert "sandbox-ai build claude" in str(refused.value) and "resume it again" in str(refused.value)
    assert sessions.fix_first("s1", {"agent": "claude"}, terminal=False) == []  # (its supervisor says it)


# --- live actions --------------------------------------------------------------------------------------------------

def test_toolchain_attach_offers_to_add_what_the_store_lacks_then_attaches(monkeypatch):
    toolchains = load("toolchains")
    monkeypatch.setattr(toolchains, "missing", lambda values, found=None: ["go@1.24"])
    offered, attached = [], []
    launcher = argparse.Namespace(toolchain_fix=load("agent_sandbox").toolchain_fix,
                                  offer_fixes=lambda error, **kw: offered.append(error.fixes) or ["toolchain:go@1.24"])
    monkeypatch.setattr(toolchains, "attach", lambda record, value, **kw: attached.append(value) or [value])
    toolchains.attach_offered({"run": "r"}, "go@1.24", out=lambda _t: None, launcher=launcher)
    assert offered[0][0]["commands"] == [["sandbox-ai", "toolchain", "add", "go@1.24"]] and attached == ["go@1.24"]


def test_offer_fixes_of_a_live_action_asks_on_a_terminal_and_refuses_elsewhere(monkeypatch):
    launcher = load("agent_sandbox")
    error = ValueError("the toolchain go is not in the store")
    error.fixes = [launcher.toolchain_fix("go")]
    with pytest.raises(ValueError) as refused:
        launcher.offer_fixes(error, about="toolchain attach go", again="attach it again", terminal=False)
    assert "  sandbox-ai toolchain add go" in str(refused.value) and "attach it again" in str(refused.value)
    assert "--fix" not in str(refused.value)


def test_kits_attach_of_a_kit_with_nothing_for_the_agent_names_how_to_make_it(monkeypatch):
    kits_live = load("kits_live")

    class Kits:
        LAYOUT = {"claude": {}}
        AGENT_NAMES = {}

        @staticmethod
        def kit_path(kit, agent):
            return Path("/nonexistent/kit")

        @staticmethod
        def kits_dir():
            return Path("/nonexistent")

        @staticmethod
        def available_kits(agent):
            return []
    monkeypatch.setattr(kits_live, "kits", lambda: Kits)
    with pytest.raises(ValueError) as refused:
        kits_live.wanted("claude", kit="house")
    item = refused.value.fixes[0]
    assert item["verdict"] == "manual" and item["commands"][0] == ["sandbox-ai", "kits", "new", "house", "--agent",
                                                                   "claude"]


def test_mcp_attach_of_a_server_not_added_carries_what_adds_it(monkeypatch):
    mcp = load("mcp")
    called = []

    class Launcher:
        @staticmethod
        def load_mcp_module():
            return mcp

        @staticmethod
        def mcp_fixes(a, module, names):
            called.append(names)
            return [fixes.fix("mcp", names[0], "not added", "add it", [["sandbox-ai", "mcp", "add", names[0]]])]
    monkeypatch.setattr(mcp, "launcher", lambda: Launcher)
    error = mcp.unknown_server(argparse.Namespace(mode="high"), "jadx", {"demo": {}})
    assert str(error) == "Unknown MCP server: jadx; defined: demo" and called == [["jadx"]]
    assert error.fixes[0]["commands"] == [["sandbox-ai", "mcp", "add", "jadx"]] and error.mode == "high"
    tries = []

    class Offering(Launcher):
        @staticmethod
        def offer_fixes(exc, **kw):
            tries.append(kw)
            return ["mcp:jadx"]
    monkeypatch.setattr(mcp, "launcher", lambda: Offering)

    def once():
        if not tries:
            raise error
        return {"attached": "next start"}
    assert mcp.attach_offered(once, "mcp attach jadx") == {"attached": "next start"}
    assert tries[0]["mode"] == "high"


# --- the agents' skill ---------------------------------------------------------------------------------------------

def test_the_agents_skill_says_a_start_offers_what_it_lacks():
    text = (ROOT / "scripts/skill_source/sandbox-ai.md.in").read_text()
    assert "offered by the start itself" in text and "`--fix`" in text
    assert "offered by the start itself" in (SANDBOX / "builtin/claude/skills/sandbox-ai/SKILL.md").read_text()
