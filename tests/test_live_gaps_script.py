"""scripts/live_check.sh and scripts/live_gaps.py without a container engine: the list of checks, --only, the
prerequisites found on a host (Podman, user namespaces, subordinate ids, Bubblewrap, images), the checks' own
environment and its Podman wrapper, the redaction, the report, the optional sections' inputs, the cleanup and a whole
run with stand-ins for Podman and pytest. The live checks themselves are tests/test_live_gaps.py (--agent-live)."""
import ast
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("live_gaps_script_test", ROOT / "scripts" / "live_gaps.py")
LG = importlib.util.module_from_spec(spec)
spec.loader.exec_module(LG)
ENTRY = ROOT / "scripts" / "live_check.sh"
TOKEN = "sai-lc-0123456789abcdef0123456789abcdef"
# Assembled here, never written whole in the source (the privacy check of a push reads it).
KEY_BLOCK = "\n".join(["-----BEGIN OPENSSH " + "PRIVATE KEY-----", "b3BlbnNzaC1rZXktdjEAAAAA",
                       "-----END OPENSSH " + "PRIVATE KEY-----"])


def args(**more):
    return SimpleNamespace(**{"host": None, "llm": None, "yes": False, "device": None,
                              "llm_models": ",".join(LG.LLM_MODELS), **more})


# --- the checks ------------------------------------------------------------------------------------------------------

def test_list_through_the_entry_point_names_each_check_with_what_it_proves_needs_and_sends():
    done = subprocess.run([str(ENTRY), "--list"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    for check in LG.CHECKS:
        assert f"\n{check.name}" in "\n" + done.stdout
    assert "(optional)" in done.stdout and "nothing leaves this machine" in done.stdout
    assert "codeberg.org" in done.stdout and "proves:" in done.stdout and "needs:" in done.stdout


def test_the_brief_s_gaps_each_have_a_check():
    names = set(LG.BY_NAME)
    assert {"release-gates", "firejail-hides-control", "firewall-login-hosts", "git-login-give", "hide-workspace-add",
            "untrusted-home", "kits-attach", "plugins-attach", "devices-scan", "devices-live", "remote-mcp",
            "local-llm"} <= names
    gates = LG.BY_NAME["release-gates"]
    assert gates.tests == ("tests/test_agent_sandbox_live.py",)
    assert gates.keyword == "isolation or strict_mode_starts or relays"
    assert {check.name for check in LG.CHECKS if check.optional} == {"remote-mcp", "local-llm", "live-suites"}
    assert LG.LLM_MODELS == ("qwen3.8-flash-next", "ornith-1.5-35b-a3b-mlx", "ornith-1.5-122b-mlx")


def test_every_test_a_check_names_exists():
    defined = {}
    for path in ("tests/test_live_gaps.py", "tests/test_agent_sandbox_live.py", "tests/test_live_macos.py"):
        tree = ast.parse((ROOT / path).read_text())
        defined[path] = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    for check in LG.CHECKS:
        for test in check.tests:
            path, _, name = test.partition("::")
            assert (ROOT / path).is_file(), test
            if name:
                assert name in defined[path], test
    gates = [name for name in defined["tests/test_agent_sandbox_live.py"] if name.startswith("test_") and any(
        word in name for word in ("isolation", "strict_mode_starts", "relays"))]
    assert len(gates) == 3, gates


def test_only_selects_comma_lists_and_repeats_and_refuses_unknown_names():
    assert [c.name for c in LG.selected(None)] == [c.name for c in LG.CHECKS if not c.optional]
    assert [c.name for c in LG.selected(["devices-scan,release-gates", "local-llm"])] == [
        "release-gates", "devices-scan", "local-llm"]
    with pytest.raises(SystemExit, match="unknown check nope; the checks: release-gates"):
        LG.selected(["nope"])
    done = subprocess.run([str(ENTRY), "--only", "nope"], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "unknown check nope" in done.stdout


def test_the_entry_point_refuses_root_and_says_why(tmp_path):
    if os.geteuid() != 0:
        pytest.skip("runs as root only")
    done = subprocess.run([str(ENTRY), "--out", str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 2 and "never with root or sudo" in done.stdout
    assert not list(tmp_path.iterdir())


# --- prerequisites -----------------------------------------------------------------------------------------------------

def host(podman=True, rootless=True, images=("codex",), bwrap=True, unshare=0, git=True):
    """A runner standing in for this host's programs."""
    calls = []

    def runner(command, timeout=60, env=None, cwd=None, input=None):
        calls.append(command)
        name = command[0]
        if name == "podman":
            if not podman:
                return 127, "", "podman: not found"
            if command[1] == "info":
                return 0, json.dumps({"version": {"Version": "5.6.1"}, "host": {
                    "cgroupVersion": "v2", "security": {"rootless": rootless}}}), ""
            if command[1:3] == ["image", "exists"]:
                return (0 if command[3].split("/")[1].split("-")[0] in images else 1), "", ""
        if name == "bwrap":
            return (0, "bubblewrap 0.11.0\n", "") if bwrap else (127, "", "bwrap: not found")
        if name == "unshare":
            return unshare, "", "unshare: write failed /proc/self/uid_map: Operation not permitted"
        if name == "git":
            return (0, "git version 2.51.0\n" if command[1] == "--version" else "abc1234\n", "") if git else \
                (127, "", "")
        return 0, "sandbox-ai 0.1.145\n", ""
    runner.calls = calls
    return runner


def proc(tmp_path, max_userns=63000, clone=None, apparmor=None):
    root = tmp_path / "proc"
    for relative, value in (("sys/user/max_user_namespaces", max_userns),
                            ("sys/kernel/unprivileged_userns_clone", clone),
                            ("sys/kernel/apparmor_restrict_unprivileged_userns", apparmor)):
        if value is not None:
            (root / relative).parent.mkdir(parents=True, exist_ok=True)
            (root / relative).write_text(f"{value}\n")
    return root


def etc(tmp_path, user="alice", subuid=True):
    root = tmp_path / "etc"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("subuid", "subgid"):
        (root / name).write_text(f"{user}:100000:65536\n" if subuid else "bob:100000:65536\n")
    (root / "os-release").write_text('NAME="Fedora"\nPRETTY_NAME="Fedora Linux 43"\n')
    return root


def found_on(tmp_path, runner, system="Linux", **kwargs):
    return LG.prerequisites({}, args(), system=system, runner=runner, proc=kwargs.get("proc") or proc(tmp_path),
                            etc=kwargs.get("etc") or etc(tmp_path), user="alice", uid=1000)


def test_a_ready_host(tmp_path):
    found, facts = found_on(tmp_path, host(images=("codex", "claude", "opencode")))
    for key in ("linux", "userns", "subuid", "podman", "bwrap", "sandbox", "git", "image:codex", "image:claude",
                "image:opencode"):
        assert found[key].ok, (key, found[key])
    assert facts["podman"] == "5.6.1" and facts["cgroup"] == "v2" and facts["distribution"] == "Fedora Linux 43"
    assert facts["sandbox-ai"] == "sandbox-ai 0.1.145"
    assert LG.backends_of("auto", found) == ["podman", "bwrap"]
    # every key a check needs is one the prerequisites answer
    for check in LG.CHECKS:
        assert set(check.needs) <= set(found), check.name


def test_a_host_without_podman_and_bubblewrap_says_what_to_install(tmp_path):
    found, _ = found_on(tmp_path, host(podman=False, bwrap=False))
    assert not found["podman"].ok and "package podman" in found["podman"].detail
    assert not found["bwrap"].ok and "package bubblewrap" in found["bwrap"].detail
    assert not found["sandbox"].ok and found["image:codex"].detail == "no Podman"
    assert LG.backends_of("auto", found) == []
    missing = LG.missing_for(LG.BY_NAME["release-gates"], found, [])
    assert missing == ["sandbox: neither Podman nor Bubblewrap can start a sandbox here"]


def test_user_namespaces_switched_off_or_without_subordinate_ids(tmp_path):
    found, _ = found_on(tmp_path, host(), proc=proc(tmp_path, max_userns=0))
    assert not found["userns"].ok and "max_user_namespaces=" in found["userns"].detail
    assert not found["podman"].ok and not found["sandbox"].ok
    found, _ = found_on(tmp_path, host(), proc=proc(tmp_path / "b", clone=0))
    assert not found["userns"].ok and "unprivileged_userns_clone=1" in found["userns"].detail
    found, _ = found_on(tmp_path, host(), etc=etc(tmp_path / "c", subuid=False))
    assert not found["subuid"].ok and "usermod --add-subuids" in found["subuid"].detail
    assert not found["podman"].ok and found["bwrap"].ok  # Bubblewrap needs no subordinate ids


def test_apparmor_restricting_namespaces_leaves_podman_usable(tmp_path):
    """Ubuntu 24.04: unshare (no profile) is refused, Podman (its profile) works."""
    found, _ = found_on(tmp_path, host(unshare=1, bwrap=True), proc=proc(tmp_path, apparmor=1))
    assert found["userns"].ok and "AppArmor" in found["userns"].detail and found["podman"].ok
    found, _ = found_on(tmp_path, host(unshare=1), proc=proc(tmp_path / "b"))
    assert not found["userns"].ok and "Operation not permitted" in found["userns"].detail


def test_podman_that_is_not_rootless_and_missing_images(tmp_path):
    found, _ = found_on(tmp_path, host(rootless=False))
    assert not found["podman"].ok and "not rootless" in found["podman"].detail
    found, _ = found_on(tmp_path, host(images=("codex",)))
    assert not found["image:claude"].ok and "sandbox-ai build claude" in found["image:claude"].detail
    assert LG.missing_for(LG.BY_NAME["kits-attach"], found, ["podman"]) == [
        "image:claude: missing: sandbox-ai build claude (or --build)"]
    # Bubblewrap alone: no image is asked for, a check for Podman alone says so
    assert LG.missing_for(LG.BY_NAME["kits-attach"], found, ["bwrap"]) == []
    assert "podman: this check runs with Podman only" in LG.missing_for(LG.BY_NAME["git-login-give"], found, ["bwrap"])


# --- on a Mac ---------------------------------------------------------------------------------------------------

def mac(machine_state="running", podman=True, tools=("hdiutil", "ditto", "codesign", "bash"), bash=True):
    """A runner standing in for a Mac's programs (sw_vers, podman and its machine), and a which() for its tools."""
    linux = host(podman=podman, bwrap=False)

    def runner(command, timeout=60, env=None, cwd=None, input=None):
        if command[0] == "sw_vers":
            return 0, "15.6.1\n", ""
        if command[0].endswith("bash"):  # /bin/bash 3.2, Homebrew's 5
            return 0, ("5\n" if command[0] == "/opt/homebrew/bin/bash" and bash else "3\n"), ""
        if command[:3] == ["podman", "machine", "inspect"]:
            if not podman:
                return 127, "", "podman: not found"
            if machine_state is None:
                return 125, "", "Error: podman-machine-default: VM does not exist"
            return 0, json.dumps([{"Name": "podman-machine-default", "State": machine_state,
                                   "Resources": {"CPUs": 6, "Memory": 12288}}]), ""
        if command[0] == "bwrap":
            raise AssertionError("Bubblewrap is never tried on a Mac")
        return linux(command, timeout, env, cwd, input)
    runner.calls = linux.calls
    return runner, (lambda tool: ("/bin/bash" if tool == "bash" else f"/usr/bin/{tool}") if tool in tools else None)


def found_on_a_mac(tmp_path, **kwargs):
    runner, which = mac(**kwargs)
    return LG.prerequisites({}, args(), system="Darwin", runner=runner, proc=proc(tmp_path), etc=etc(tmp_path),
                            user="alice", uid=501, which=which)


def test_a_mac_runs_the_macos_checks_and_skips_the_linux_ones(tmp_path):
    found, facts = found_on_a_mac(tmp_path)
    assert found["macos"].ok and "macOS 15.6.1" in found["macos"].detail
    assert "bash 5 (/opt/homebrew/bin/bash)" in found["macos"].detail
    assert facts["macos"] == "15.6.1" and facts["distribution"] == "macOS"
    assert found["podman-machine"].ok and "podman-machine-default running, 6 CPUs" == found["podman-machine"].detail
    assert not found["linux"].ok and "on a Mac the macos-* checks run" in found["linux"].detail
    assert not found["podman"].ok and "podman machine's Linux VM" in found["podman"].detail
    assert not found["bwrap"].ok and not found["sandbox"].ok
    assert LG.backends_of("auto", found) == []
    assert LG.missing_for(LG.BY_NAME["devices-scan"], found, []) == [
        "linux: Darwin: this check needs Linux (on a Mac the macos-* checks run)"]
    assert LG.missing_for(LG.BY_NAME["macos-doctor"], found, []) == []
    assert LG.missing_for(LG.BY_NAME["macos-devices"], found, []) == []
    assert LG.missing_for(LG.BY_NAME["macos-update"], found, [])[0].startswith("dmgs: give --dmg-old DMG --dmg-new")
    assert LG.missing_for(LG.BY_NAME["macos-app"], found, [])[0].startswith("app: give --app PATH")
    for check in LG.CHECKS:  # every key a check needs is one the prerequisites answer on a Mac too
        assert set(check.needs) <= set(found), check.name


def test_a_mac_without_its_tools_or_its_podman_machine(tmp_path):
    found, _ = found_on_a_mac(tmp_path, tools=("hdiutil", "ditto", "bash"))
    assert not found["macos"].ok and "codesign not found" in found["macos"].detail
    found, _ = found_on_a_mac(tmp_path, bash=False)
    assert not found["macos"].ok and "brew install bash" in found["macos"].detail
    found, _ = found_on_a_mac(tmp_path, machine_state="stopped")
    assert not found["podman-machine"].ok and found["podman-machine"].detail.endswith("podman machine start")
    found, _ = found_on_a_mac(tmp_path, machine_state=None)
    assert not found["podman-machine"].ok and "podman machine init" in found["podman-machine"].detail
    found, _ = found_on_a_mac(tmp_path, podman=False)
    assert not found["podman-machine"].ok and "brew install podman" in found["podman-machine"].detail
    assert LG.missing_for(LG.BY_NAME["macos-doctor"], found, []) == [], "doctor's verdict is checked either way"


def test_on_linux_the_macos_checks_say_they_run_on_a_mac(tmp_path):
    found, _ = found_on(tmp_path, host())
    assert not found["macos"].ok
    for name in ("macos-doctor", "macos-devices", "macos-app", "macos-update"):
        assert LG.missing_for(LG.BY_NAME[name], found, ["podman"])[0] == "macos: Linux: the macOS checks run on a Mac"
    assert {check.name for check in LG.MAC_CHECKS} == {"macos-doctor", "macos-devices", "macos-app", "macos-update"}
    assert not any(check.network for check in LG.MAC_CHECKS), "the macOS checks send nothing from the Mac"


# --- the checks' own environment -----------------------------------------------------------------------------------

def test_the_environment_has_folders_of_its_own_and_never_the_users_settings(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "podman").write_text("#!/bin/sh\necho \"HOME=$HOME DATA=${XDG_DATA_HOME-unset} "
                                 "CONFIG=${XDG_CONFIG_HOME-unset} RUN=${XDG_RUNTIME_DIR-unset} ARGS=$*\"\n")
    (fake / "podman").chmod(0o755)
    base = {"HOME": "/home/alice", "PATH": f"{fake}:/usr/bin:/bin", "XDG_RUNTIME_DIR": "/run/user/1000",
            "SANDBOX_AI_RUN": "work", "SANDBOX_AI_PODMAN_URL": "unix:///x.sock", "SANDBOX_AI_SECRETS": "keyring",
            "LANG": "C.UTF-8"}
    env = LG.isolated_environment(tmp_path / "e", base)
    for name, folder in (("HOME", "home"), ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
                         ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "run")):
        assert env[name] == str(tmp_path / "e" / folder) and Path(env[name]).is_dir()
        assert (Path(env[name]).stat().st_mode & 0o777) == 0o700
    assert "SANDBOX_AI_RUN" not in env and env["SANDBOX_AI_PODMAN_URL"] == "unix:///x.sock"
    assert env["SANDBOX_AI_SECRETS"] == "file" and env["SANDBOX_AI_SECRETS_PLAIN"] == "1"
    assert env["PATH"].split(":")[0] == str(tmp_path / "e" / "bin") and env["LANG"] == "C.UTF-8"
    # Podman, through the wrapper, sees the user's HOME and runtime folder; XDG folders the user had none of: unset.
    done = subprocess.run(["podman", "image", "exists", "x"], capture_output=True, text=True, env=env, timeout=30)
    assert done.stdout.strip() == "HOME=/home/alice DATA=unset CONFIG=unset RUN=/run/user/1000 ARGS=image exists x"


def test_a_wrapper_of_a_wrapper_still_reaches_the_users_podman(tmp_path):
    """A live test inside the script's run makes its own environment from the script's: Podman still gets the
    user's HOME."""
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "podman").write_text("#!/bin/sh\necho \"HOME=$HOME\"\n")
    (fake / "podman").chmod(0o755)
    outer = LG.isolated_environment(tmp_path / "a", {"HOME": "/home/alice", "PATH": f"{fake}:/usr/bin:/bin"})
    inner = LG.isolated_environment(tmp_path / "b", outer)
    done = subprocess.run(["podman"], capture_output=True, text=True, env=inner, timeout=30)
    assert done.stdout.strip() == "HOME=/home/alice"


# --- redaction -------------------------------------------------------------------------------------------------------

def test_secrets_and_home_paths_are_replaced():
    r = LG.Redactor("/home/alice", ["/tmp/sai-lc-x1"], [TOKEN], environ={"GITEA_TOKEN": "s3cr3t-value-123",
                                                                         "PATH": "/usr/bin"})
    text = "\n".join([
        f"token {TOKEN} in a message", "/home/alice/src/sandbox-ai/tests/test_x.py:12", "/home/alice",
        "/home/bob/.ssh/id_ed25519", "/Users/carol/Library", "/tmp/sai-lc-x1/p/state/sessions/lc-1/tmux",
        "ghp_" + "A1b2C3d4" * 4 + "E5f6", "Authorization: Bearer abc.def.ghi-jkl", "password=hunter2hunter",
        '"api_key": "sk-proj-abcdefghijklmnopqrstuv"', "https://alice:" + "pa55word" + "@git.example.org/r.git",
        KEY_BLOCK,
        "uses s3cr3t-value-123 here", "max_tokens: 131072", "--git-token-env https://codeberg.org=SAI_LC_GIT_TOKEN",
        "/home/alicetwo/x"])
    clean = r.text(text)
    for secret in (TOKEN, "A1b2C3d4", "abc.def.ghi", "hunter2", "sk-proj", "pa55word", "b3BlbnNzaC1", "s3cr3t-value"):
        assert secret not in clean, secret
    for kept in ("~/src/sandbox-ai/tests/test_x.py:12", "/home/<user>/.ssh/id_ed25519", "/Users/<user>/Library",
                 "<tmp>/p/state/sessions/lc-1/tmux", "<private key>", "Authorization: <secret>",
                 "https://alice:<secret>@git.example.org", "max_tokens: 131072",
                 "--git-token-env https://codeberg.org=SAI_LC_GIT_TOKEN", "/home/<user>/x"):
        assert kept in clean, (kept, clean)
    assert "\n~\n" in clean and "alice" not in clean.replace("alice:<secret>", "")
    data = r.data({"checks": [{"tail": [f"x {TOKEN}"], "n": 3}], "/home/alice/k": ("a", "/home/alice/b")})
    assert data == {"checks": [{"tail": ["x <secret>"], "n": 3}], "~/k": ["a", "~/b"]}


# --- running a check -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("status, outcomes, expected", [
    (0, ["passed", "passed"], "passed"), (0, ["passed", "skipped"], "passed"), (0, ["skipped"], "skipped"),
    (1, ["passed", "failed"], "failed"), (1, ["error"], "failed"), (5, [], "skipped"), (2, [], "error"),
    (3, ["passed"], "error"), (124, ["passed"], "failed"), (124, [], "failed")])
def test_the_outcome_of_a_pytest_run(status, outcomes, expected):
    assert LG.outcome_of(status, [(f"t{i}", outcome, "", 0) for i, outcome in enumerate(outcomes)]) == expected


def test_the_pytest_command_of_a_check(tmp_path):
    command = LG.pytest_command("/py", LG.BY_NAME["release-gates"], "podman", tmp_path / "b", tmp_path / "j.xml")
    assert command[:5] == ["/py", "-X", "faulthandler", "-m", "pytest"]
    assert "--agent-live" in command and command[command.index("--agent-backend") + 1] == "podman"
    assert command[-3:] == ["-k", "isolation or strict_mode_starts or relays", "tests/test_agent_sandbox_live.py"]
    assert f"--junitxml={tmp_path / 'j.xml'}" in command and f"--basetemp={tmp_path / 'b'}" in command
    suites = LG.pytest_command("/py", LG.BY_NAME["live-suites"], "both", "b", "j", ["tests/a.py"], "not login")
    assert suites[-3:] == ["-k", "not login", "tests/a.py"]
    one = LG.pytest_command("/py", LG.BY_NAME["devices-scan"], "both", "b", "j")
    assert one[-1] == "tests/test_live_gaps.py::test_live_devices_scan_on_this_machine"


def test_a_check_that_runs_too_long_is_ended_with_its_whole_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    command = ["sh", "-c", f"sleep 120 & echo $! > {pid_file}; wait"]
    status, seconds = LG.run_logged(command, tmp_path / "log.txt", 1, dict(os.environ), tmp_path)
    assert status == 124 and seconds < 60
    assert "timed out after 1 s" in (tmp_path / "log.txt").read_text()
    pid = int(pid_file.read_text())
    for _ in range(50):
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split(")")[1].split()[0]
        except OSError:
            state = "gone"
        if state in ("gone", "Z"):
            break
        time.sleep(0.1)
    assert state in ("gone", "Z"), state  # its child too: the whole group was ended


def test_sessions_left_behind_are_found_with_their_folders_and_stopped(tmp_path):
    for test, name in (("p/a/t0/e", "lc-aaaaaa"), ("p/b/t1/e", "lc-bbbbbb")):
        record = tmp_path / test / "state" / "sandbox-ai" / "sessions" / name / "session.json"
        record.parent.mkdir(parents=True)
        record.write_text("{}")
        (tmp_path / test / "config").mkdir()
        (tmp_path / test / "run").mkdir()
    found = LG.leftover_sessions(tmp_path)
    assert [name for _, name in found] == ["lc-aaaaaa", "lc-bbbbbb"]
    folders = found[0][0]
    assert folders == {"XDG_STATE_HOME": str(tmp_path / "p/a/t0/e/state"),
                       "XDG_CONFIG_HOME": str(tmp_path / "p/a/t0/e/config"),
                       "XDG_RUNTIME_DIR": str(tmp_path / "p/a/t0/e/run")}
    calls = []
    stopped = LG.stop_leftovers(tmp_path, {"PATH": "/usr/bin"}, runner=lambda command, timeout, env: calls.append(
        (command[1:], env.get("XDG_STATE_HOME"))) or (0, "", ""))
    assert stopped == ["lc-aaaaaa", "lc-bbbbbb"]
    assert calls[0] == (["session", "stop", "lc-aaaaaa"], str(tmp_path / "p/a/t0/e/state"))
    assert calls[1] == (["session", "delete", "lc-aaaaaa", "--yes"], str(tmp_path / "p/a/t0/e/state"))
    assert LG.leftover_sessions(tmp_path / "nothing") == []


# --- the optional sections' inputs ---------------------------------------------------------------------------------

def test_an_llm_server_is_taken_without_its_secrets_or_refused(tmp_path):
    config = tmp_path / "config"
    (config / "llm").mkdir(parents=True)
    (config / "llm" / "lab.pem").write_text("CERT")
    (config / "llm.json").write_text(json.dumps({"version": 1, "endpoints": {
        "mac": {"kind": "lmstudio", "url": "http://192.168.1.20:1234", "checked": "x", "contexts": {}},
        "lab": {"kind": "vllm", "url": "https://lab:8000", "ca": "lab.pem"},
        "cloud": {"kind": "openai", "url": "https://api.example.org", "api_key_secret": "CLOUD_KEY"},
        "tunnel": {"kind": "ollama", "url": "http://10.0.0.2:11434", "vpn": "work"}}}))
    entry, problem = LG.llm_endpoint("mac", config)
    assert problem is None and entry == {"name": "mac", "record": {"kind": "lmstudio",
                                                                  "url": "http://192.168.1.20:1234"}, "ca": None}
    entry, problem = LG.llm_endpoint("lab", config)
    assert problem is None and entry["ca"] == str(config / "llm" / "lab.pem")
    for name, word in (("cloud", "api_key_secret"), ("tunnel", "vpn")):
        entry, problem = LG.llm_endpoint(name, config)
        assert entry is None and word in problem and "never copied" in problem
    assert "configured: cloud, lab, mac, tunnel" in LG.llm_endpoint("nope", config)[1]
    assert LG.llm_endpoint("mac", tmp_path / "none")[1] == "no LLM server is configured (sandbox-ai llm add)"
    assert LG.llm_names(config) == ["cloud", "lab", "mac", "tunnel"] and LG.llm_names(tmp_path) == []


def test_a_host_is_copied_alone_with_sandbox_ais_known_hosts(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "hosts.json").write_text(json.dumps({"version": 1, "hosts": {
        "box": {"ssh": "me@box.example"}, "other": {"ssh": "you@other.example"}}}))
    (config / "known_hosts").write_text("box.example ssh-ed25519 AAAA\n")
    target = tmp_path / "given"
    assert LG.host_files("box", config, target) is None
    assert json.loads((target / "hosts.json").read_text()) == {"version": 1, "hosts": {"box": {"ssh": "me@box.example"}}}
    assert (target / "known_hosts").read_text() == "box.example ssh-ed25519 AAAA\n"
    assert (target / "hosts.json").stat().st_mode & 0o777 == 0o600
    assert "configured: box, other" in LG.host_files("nope", config, target)
    assert LG.host_files("box", tmp_path / "none", target) == "no host is configured (sandbox-ai hosts add)"
    assert LG.host_names(config) == ["box", "other"]


def test_the_device_of_devices_live():
    scan = {"devices": [
        {"kind": "usb", "value": "usb:0403:6001", "option": "--device", "refused": None},
        {"kind": "kvm", "value": "/dev/kvm", "option": "--device", "refused": None},
        {"kind": "serial", "value": "/dev/ttyUSB0", "option": "--device", "refused": "needs a udev rule"},
        {"kind": "render", "value": "/dev/dri/renderD128", "option": "--device", "refused": None},
        {"kind": "gpu", "value": "dri", "option": "--gpu", "refused": None}]}
    assert LG.pick_device(scan) == "/dev/dri/renderD128"  # the serial port is refused; a render node before kvm
    assert LG.pick_device({"devices": scan["devices"][:2]}) == "/dev/kvm"
    assert LG.pick_device({"devices": []}) is None and LG.pick_device(None) is None
    given = args(device="/dev/ttyACM0")
    env = {}
    assert LG.prepare_device(given, env).ok and env["SAI_LIVE_DEVICE"] == "/dev/ttyACM0"
    env = {}
    found = LG.prepare_device(args(), env, runner=lambda *a, **k: (0, json.dumps({"devices": []}), ""))
    assert not found.ok and "give --device PATH" in found.detail and "SAI_LIVE_DEVICE" not in env


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_questions_are_asked_on_a_terminal_only():
    said = io.StringIO()
    assert LG.ask("Run them?", Terminal("y\n"), said) is True and said.getvalue() == "Run them? [y/N] "
    assert LG.ask("Run them?", Terminal("\n"), io.StringIO()) is False
    assert LG.ask("Run them?", io.StringIO("y\n"), io.StringIO()) is False  # no terminal: never yes


def test_the_llm_section_asks_and_hands_the_endpoint_over(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "llm.json").write_text(json.dumps({"version": 1, "endpoints": {
        "mac": {"kind": "lmstudio", "url": "http://192.168.1.20:1234"}}}))
    env = {}
    problem = LG.prepare_llm(args(), config, tmp_path / "t", env, io.StringIO(""))
    assert problem.startswith("not run: not confirmed") and "SAI_LIVE_LLM_ENDPOINT" not in env
    given = args()
    assert LG.prepare_llm(given, config, tmp_path / "t", env, Terminal("yes\n")) is None
    assert json.loads(Path(env["SAI_LIVE_LLM_ENDPOINT"]).read_text())["name"] == "mac"
    assert env["SAI_LIVE_LLM_MODELS"] == ",".join(LG.LLM_MODELS) and given.llm_said.startswith("mac: qwen3.8")
    (config / "llm.json").write_text(json.dumps({"version": 1, "endpoints": {"a": {}, "b": {}}}))
    assert LG.prepare_llm(args(), config, tmp_path / "t", {}) == "give --llm NAME (configured: a, b)"


# --- the report and a whole run --------------------------------------------------------------------------------------

def test_the_report_in_markdown_and_json_redacted(tmp_path):
    report = {"version": 1, "date": "20261119-1200", "facts": {"kernel": "6.12", "checkout": "/home/alice/sai"},
              "prerequisites": [{"name": "podman", "ok": True, "detail": "podman 5.6.1"},
                                {"name": "image:claude", "ok": False, "detail": "missing | build"}],
              "network": ["firewall-login-hosts: HTTPS to codeberg.org"],
              "checks": [{"name": "release-gates", "proves": "gates", "outcome": "passed", "seconds": 61.2,
                          "command": "/usr/bin/python3 -m pytest", "missing": [], "tests": [
                              {"name": "t::a", "outcome": "passed", "seconds": 3, "detail": ""}], "tail": ["ok"]},
                         {"name": "firejail-hides-control", "proves": "hidden", "outcome": "failed", "seconds": 9,
                          "command": "pytest", "missing": [], "tests": [
                              {"name": "t::b", "outcome": "failed", "seconds": 9,
                               "detail": f"AssertionError: /home/alice/x {TOKEN}"}],
                          "tail": [f"E  {TOKEN} at /home/alice/y"]},
                         {"name": "kits-attach", "proves": "kits", "outcome": "skipped", "seconds": None,
                          "missing": ["image:claude: missing"], "tests": [], "tail": []}]}
    md, js = LG.write_report(report, tmp_path / "out", LG.Redactor("/home/alice", [], [TOKEN], environ={}), "X")
    assert md.name == "REPORT-X.md" and js.name == "REPORT-X.json"
    text, data = md.read_text(), json.loads(js.read_text())
    assert TOKEN not in text + js.read_text() and "/home/alice" not in text + js.read_text()
    assert data["counts"] == {"passed": 1, "failed": 1, "skipped": 1}
    assert data["checks"][1]["tests"][0]["detail"] == "AssertionError: ~/x <secret>"
    assert "| passed | release-gates | 61 | gates |" in text and "| failed | firejail-hides-control | 9 |" in text
    assert "| missing | image:claude | missing \\| build |" in text
    assert "### firejail-hides-control: failed" in text and "- failed: `t::b` (9 s)" in text
    assert "AssertionError: ~/x <secret>" in text and "E  <secret> at ~/y" in text
    assert "Missing: image:claude: missing" in text and "## What left this machine" in text
    assert "| checkout | ~/sai |" in text


JUNIT = """<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">{}</testsuite></testsuites>"""


def test_a_whole_run_with_stand_ins_for_podman_and_pytest(tmp_path, monkeypatch, capsys):
    """main() as the entry point runs it, on a host whose Podman is a stand-in (codex image only) and whose pytest
    runs are faked: the checks that lack something are skipped with what is missing, a failing check is in the
    report with its redacted detail, the exit status is 0, the checks' folder is removed, nothing of the real HOME
    is used."""
    home = tmp_path / "home" / "alice"
    (home / ".config" / "sandbox-ai").mkdir(parents=True)
    (home / ".config" / "sandbox-ai" / "llm.json").write_text(json.dumps({"version": 1, "endpoints": {
        "mac": {"kind": "lmstudio", "url": "http://192.168.1.20:1234"}}}))
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "podman").write_text(
        "#!/bin/sh\n"
        "case \"$1 $2\" in\n"
        "  'info --format') echo '{\"version\": {\"Version\": \"5.6.1\"}, \"host\": {\"cgroupVersion\": \"v2\", "
        "\"security\": {\"rootless\": true}}}' ;;\n"
        "  'image exists') case \"$3\" in *codex*) exit 0 ;; *) exit 1 ;; esac ;;\n"
        "esac\n")
    (fake / "unshare").write_text("#!/bin/sh\nexit 0\n")
    for name in ("podman", "unshare"):
        (fake / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:/usr/bin:/bin")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.setattr(LG.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(LG, "subordinate_ids", lambda *a, **k: LG.Found(True, "ranges"))
    real = LG.load_live_check()
    monkeypatch.setattr(real, "inside_a_container", lambda: False)
    monkeypatch.setattr(real, "pytest_python", lambda directory: sys.executable)
    monkeypatch.setattr(LG, "load_live_check", lambda: real)
    # A stand-in for `devices scan` too: one offering nothing (GitHub's runner has a /dev/kvm the real scan offers,
    # and devices-live ran there).
    prepare_device = LG.prepare_device
    monkeypatch.setattr(LG, "prepare_device", lambda args, env: prepare_device(
        args, env, runner=lambda command, **kwargs: (0, json.dumps({"devices": []}), "")))
    seen = {}

    def fake_pytest(command, log, timeout, env, cwd):
        junit = Path(next(part for part in command if part.startswith("--junitxml="))[len("--junitxml="):])
        name = junit.stem
        seen[name] = {"command": command, "env": env}
        if name == "firejail-hides-control":
            case = (f'<testcase classname="tests.test_live_gaps" name="test_x[podman]" time="9"><failure message="'
                    f'AssertionError: {env["SAI_LIVE_TOKEN"]} at {home}/w">assert False</failure></testcase>')
            status = 1
        else:
            case = f'<testcase classname="tests.test_live_gaps" name="test_{name}" time="2"/>'
            status = 0
        junit.write_text(JUNIT.format(case))
        Path(log).write_text(f"collected 1 item\n{env['HOME']}/thing {env['SAI_LIVE_TOKEN']}\n1 passed\n")
        return status, 2.0
    monkeypatch.setattr(LG, "run_logged", fake_pytest)
    lines = []
    status = LG.main(["--out", str(tmp_path / "out"), "--work-dir", str(tmp_path)], out=lines.append,
                     stdin=io.StringIO(""))
    assert status == 0
    report = json.loads(next((tmp_path / "out").glob("REPORT-*.json")).read_text())
    outcomes = {check["name"]: check["outcome"] for check in report["checks"]}
    assert outcomes["release-gates"] == "passed" and outcomes["firejail-hides-control"] == "failed"
    assert outcomes["kits-attach"] == "skipped" and outcomes["plugins-attach"] == "skipped"
    assert outcomes["devices-live"] == "skipped" and outcomes["local-llm"] == "skipped"
    assert "remote-mcp" not in outcomes and "live-suites" not in outcomes
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["kits-attach"]["missing"] == ["image:claude: missing: sandbox-ai build claude (or --build)"]
    assert checks["local-llm"]["missing"][0].startswith("llm: not run: not confirmed")
    assert checks["devices-live"]["missing"] == ["device: devices scan offers no serial port, camera, render node or "
                                                 "/dev/kvm to hand over; give --device PATH"]
    assert checks["git-login-give"]["outcome"] == "passed" and "--agent-backend podman" in checks[
        "git-login-give"]["command"]
    text = next((tmp_path / "out").glob("REPORT-*.md")).read_text()
    for env in (entry["env"] for entry in seen.values()):
        assert env["SAI_LIVE_TOKEN"] not in text and env["HOME"] != str(home)
        assert env["SANDBOX_AI_SECRETS"] == "file" and env["PATH"].startswith(env["HOME"].rsplit("/", 1)[0])
    assert str(home) not in text and str(tmp_path) not in text.replace("<tmp>", "")
    assert "<tmp>/home/thing <secret>" in text
    assert not [path for path in tmp_path.glob("sai-lc-*")], "the checks' folder is removed"
    said = "\n".join(lines)
    assert "What these checks send from this machine:" in said and "codeberg.org" in said
    assert "failed 1" in said and "report:" in said


def test_docs_live_check_names_every_check_and_option():
    """docs/LIVE_CHECK.md: each check in a table row of its own, each option of the script in the options table."""
    text = (ROOT / "docs" / "LIVE_CHECK.md").read_text()
    for check in LG.CHECKS:
        assert f"| `{check.name}`" in text, check.name
    for action in LG.parser()._actions:
        for option in action.option_strings:
            if option.startswith("--") and option != "--help":
                assert f"`{option}" in text, option


def test_a_pytest_only_in_the_users_site_gets_a_venv_of_its_own(tmp_path, monkeypatch):
    """User 2026-11-21 (Arch, /usr/bin/python3.14, pytest from `pip install --user`): every check ended in 0 s with
    "No module named pytest" - the checks run with a HOME of their own, where the real HOME's user site is not found,
    and pytest_python had taken this Python's pytest as found. Now it is looked for outside the user site (-s)."""
    real = LG.load_live_check()
    calls = []

    def run(command, timeout=None, **_):
        calls.append(command)
        if command[1:3] == ["-s", "-c"]:
            return 1, "", "ModuleNotFoundError: No module named 'pytest'"  # only in ~/.local: not found with -s
        if command[1:3] == ["-m", "venv"]:
            (Path(command[3]) / "bin").mkdir(parents=True)
            (Path(command[3]) / "bin" / "python").write_text("")
        return 0, "", ""
    monkeypatch.setattr(real, "run", run)
    python = real.pytest_python(tmp_path)
    assert python == str(tmp_path / "venv" / "bin" / "python") and python != sys.executable
    assert calls[0] == [sys.executable, "-s", "-c", "import pytest"]
    assert any(command[1:3] == ["-m", "pip"] and "--require-hashes" in command for command in calls)
    calls.clear()
    monkeypatch.setattr(real, "run", lambda command, timeout=None, **_: calls.append(command) or (0, "", ""))
    assert real.pytest_python(tmp_path / "other") == sys.executable, "a pytest outside the user site is this one's"
    status_found = LG.pytest_found(tmp_path / "none")
    assert status_found.ok


def test_a_checks_basetemp_has_its_parent_when_pytest_starts(tmp_path, monkeypatch):
    """User 2026-11-21: every check failed on setup, "No such file or directory: '<tmp>/p/releasegates'" - pytest
    makes --basetemp, never its parents, and <tmp>/p was not there. The whole-run test fakes pytest, so it never
    showed."""
    seen = {}

    def run_logged(command, log, timeout, env, cwd):
        basetemp = Path(next(part for part in command if str(part).startswith("--basetemp="))[len("--basetemp="):])
        seen["parent"] = basetemp.parent.is_dir()
        seen["exists"] = basetemp.exists()
        return 0, 0.1
    monkeypatch.setattr(LG, "run_logged", run_logged)
    lc = SimpleNamespace(SUITES={}, NEEDS_LOGIN=set(), junit_results=lambda path: [])
    check = next(c for c in LG.CHECKS if c.name == "release-gates")
    entry = {}
    LG.run_check(check, entry, sys.executable, ["podman"], tmp_path / "t", {}, lc, lambda line: None)
    assert seen == {"parent": True, "exists": False}, seen  # its parent made; the folder itself is pytest's to make
