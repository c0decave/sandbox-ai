"""Mounts: you decide. "If the human wants to mount something in, no matter what it is, it is done - with a warning
that it is a risk." Every host path the human chooses (--workspace, --mount, --state-dir, --copy, --log-dir, a
workspace added during the run, an MCP server's folder) that sandbox-ai refused before is given now, with a warning
that names the concrete risk and the safer read-only choice - on standard error and in launcher.log. What stays
refused: what is technically impossible, and what the human did not choose (a link an agent may have planted on the
way, a preset passed on by someone else). Each start here is a dry run of the real ./sandbox-ai entry point
(live_fakes.py)."""
import json
import os
from pathlib import Path
import socket
import tempfile

import pytest

from live_fakes import ROOT, Host


@pytest.fixture
def host(tmp_path):
    host = Host(tmp_path)
    (host.home / ".config/sandbox-ai").mkdir(parents=True, exist_ok=True)
    (host.home / ".config/sandbox-ai/defaults.json").write_text('{"version": 1}')
    return host


def given(host, *options):
    """The dry run of `sandbox-ai claude OPTIONS`: (its plan, what it said on standard error), asserting it was
    given."""
    result = host.run("claude", "--dry-run", *map(str, options))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout), result.stderr


def warnings(text):
    """The launcher's warnings in TEXT, one line each."""
    return [line for line in text.splitlines() if line.startswith("agent-sandbox: warning: ")]


def logged(host):
    path = host.home / ".local/state/sandbox-ai/launcher.log"
    return path.read_text() if path.exists() else ""


def mounted(plan, path):
    """PATH is among the host paths the plan gives the sandbox (its workspaces and mounts)."""
    return str(path) in json.dumps(plan)


# --- sandbox-ai's own folders ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("option", ["--workspace", "--mount", "--state-dir"])
def test_sandbox_ais_own_folder_is_given_with_a_warning(host, option):
    """--workspace, --mount and --state-dir in sandbox-ai's own folders were refused ("a sandbox given it could change
    sandbox-ai itself"): now given, and the warning names what the agent can change and that read-only is safer."""
    own = host.home / ".local/state/sandbox-ai"
    own.mkdir(parents=True, exist_ok=True)
    value = f"{own}:/mnt/own:rw" if option == "--mount" else own
    plan, said = given(host, option, value)
    assert mounted(plan, own)
    text = " ".join(line for line in warnings(said) if "sandbox-ai's own folder" in line)
    assert text, said
    for words in ("the agent can change sandbox-ai itself", "approvals", "network rules", "trust store",
                  "read-only"):
        assert words in text, text
    assert "sandbox-ai's own folder" in logged(host)


def test_a_read_only_mount_of_sandbox_ais_own_folder_says_what_it_can_read(host):
    own = host.home / ".config/sandbox-ai"
    plan, said = given(host, "--mount", f"{own}:/mnt/own:ro")
    assert mounted(plan, own)
    text = " ".join(warnings(said))
    assert "sandbox-ai's own folder" in text and "request logs" in text and "approvals" in text, said


def test_copy_from_sandbox_ais_secret_store_is_given_with_a_warning(host):
    secret = host.home / ".local/share/sandbox-ai/secrets/OPENAI_API_KEY"
    secret.parent.mkdir(parents=True)
    secret.write_text("sk-x")
    _, said = given(host, "--copy", f"{secret}:key")
    assert any("--copy" in line and "sandbox-ai's own folder" in line for line in warnings(said)), said


# --- HOME, the root, the temporary directory, XDG_RUNTIME_DIR ------------------------------------------------------------

def test_home_as_the_workspace_is_given_with_a_warning(host):
    plan, said = given(host, "--workspace", host.home)
    assert plan["workspace"] == str(host.home) if "workspace" in plan else mounted(plan, host.home)
    text = " ".join(line for line in warnings(said) if "your HOME directory" in line)
    for words in ("shell start files", "credentials", "trust store", "everything you can do", "read-only"):
        assert words in text, said
    # What it holds of sandbox-ai's own (its folders, the files the launcher reads) is said, never refused.
    assert "sandbox-ai's own folder" in said and "the launcher reads" in said, said


def test_the_root_directory_as_the_workspace_is_given_with_a_warning(host):
    plan, said = given(host, "--workspace", "/")
    assert mounted(plan, "/")
    text = " ".join(line for line in warnings(said) if "the root directory" in line)
    assert "everything you can do" in text and "read-only" in text, said


def test_the_temporary_directory_as_the_workspace_is_given_with_a_warning(host):
    temporary = Path(tempfile.gettempdir()).resolve()
    plan, said = given(host, "--workspace", temporary)
    assert mounted(plan, temporary)
    assert any("the temporary directory" in line for line in warnings(said)), said


def test_xdg_runtime_dir_as_the_workspace_is_given_with_a_warning(host):
    plan, said = given(host, "--workspace", host.runtime)
    assert mounted(plan, host.runtime)
    text = " ".join(line for line in warnings(said) if "XDG_RUNTIME_DIR" in line)
    assert "socket" in text and "read-only" in text, said


# --- the launcher's own tree ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("option", ["--workspace", "--mount", "--state-dir"])
def test_the_launchers_tree_is_given_writable_with_a_warning(host, option):
    tree = ROOT / "sandbox"
    value = f"{tree}:/mnt/launcher:rw" if option == "--mount" else tree
    plan, said = given(host, option, value)
    assert mounted(plan, tree)
    text = " ".join(line for line in warnings(said) if "sandbox-ai's own launcher" in line)
    assert "the code that runs on your host as you" in text and "read-only" in text, said


def test_the_launchers_tree_read_only_says_nothing_of_it(host):
    _, said = given(host, "--mount", f"{ROOT / 'sandbox'}:/mnt/launcher:ro")
    assert not any("sandbox-ai's own launcher" in line for line in warnings(said)), said


# --- the kernel's and the devices' trees ---------------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["/proc:/mnt/hostproc:ro", "/sys:/mnt/hostsys:ro", "/dev:/mnt/hostdev:ro"])
def test_a_kernel_tree_is_given_with_a_warning(host, value):
    plan, said = given(host, "--mount", value)
    assert mounted(plan, value.split(":")[0])
    text = " ".join(line for line in warnings(said) if value.split(":")[0] in line)
    assert "kernel" in text and "Podman or Bubblewrap" in text, said


def test_proc_as_the_workspace_is_given_with_a_warning(host):
    plan, said = given(host, "--workspace", "/proc")
    assert mounted(plan, "/proc")
    assert any("/proc" in line and "kernel" in line for line in warnings(said)), said


def test_a_socket_is_mounted_with_a_warning(host):
    """A socket file was never mounted ("whoever connects to it gets what runs behind it"): given, said so."""
    folder = Path(tempfile.mkdtemp(prefix="s", dir="/tmp"))
    path = folder / "a.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        listener.listen(1)
        plan, said = given(host, "--mount", f"{path}:/mnt/a.sock:rw")
        assert mounted(plan, path)
        assert any("socket" in line and "even through a read-only mount" in line for line in warnings(said)), said
        # A folder holding one: the same.
        plan, said = given(host, "--mount", f"{folder}:/mnt/sockets:ro")
        assert mounted(plan, folder)
        assert any("holds the socket" in line for line in warnings(said)), said
    finally:
        listener.close()
        path.unlink()
        folder.rmdir()


# --- what the launcher reads in a tree the agent can write ------------------------------------------------------------

def test_a_file_the_launcher_reads_inside_the_workspace_is_said(host):
    """--rules FILE inside the workspace: given, and the warning says that a link the agent puts there could make the
    launcher act on another file."""
    rules = host.workspace / "rules.json"
    rules.write_text(json.dumps({"version": 1}))
    result = host.run("claude", "--dry-run", "--workspace", host.workspace, "--rules", rules)
    text = " ".join(line for line in warnings(result.stderr) if "rules.json" in line)
    assert "which the agent can change" in text and "link" in text and "another file" in text, result.stderr


def test_remembered_approvals_in_a_tree_the_agent_can_write_are_read_with_a_warning(host):
    """The user's rule for every file of sandbox-ai's own in a folder they chose (2026-11-16: "read all, warn"): the
    remembered approvals ("always" for this project) inside the workspace were left unread for the start, unlike
    the trust store, the defaults file and the kit hook approvals beside them; now they are applied too, and the
    warning says the agent could have approved its own wishes there."""
    config = host.home / ".config/sandbox-ai"
    (config / "approved.json").write_text(json.dumps({str(host.home.resolve()): {"hosts": ["approved.example.org"]}}))
    plan, said = given(host, "--workspace", host.home)
    assert "approved.example.org" in json.dumps(plan), "the remembered approval is applied"
    text = " ".join(line for line in warnings(said) if "approved.json" in line)
    assert "Remembered approvals" in text and "which the agent can change" in text, said


def test_a_link_planted_on_the_way_to_what_the_launcher_reads_stays_refused(host):
    """Not a human choice: a link in a tree a sandbox could write in an earlier run, on the way to a file the launcher
    reads - sandbox-ai never follows it."""
    state = host.home / ".local/state/sandbox-ai"
    state.mkdir(parents=True, exist_ok=True)
    (state / "exposed.json").write_text(json.dumps([str(host.home)]))
    real = host.directory / "real-config"
    (host.home / ".config/sandbox-ai").rename(real)
    (host.home / ".config/sandbox-ai").symlink_to(real)
    result = host.run("claude", "--dry-run", "--workspace", host.home)
    assert result.returncode == 1, result.stderr
    assert "goes through the link" in result.stderr and "does not follow" in result.stderr, result.stderr


# --- what stays as it was ------------------------------------------------------------------------------------------------

def test_a_preset_passed_on_still_may_not_mount_sandbox_ais_own_folders(host):
    """Not the human's choice: a preset of someone else's (a team's directory) naming sandbox-ai's own folder."""
    where = host.directory / "team-presets"
    where.mkdir()
    (where / "grab.json").write_text(json.dumps({
        "format": "sandbox-ai preset", "version": 1, "id": "grab", "title": "Grab", "description": "d",
        "agents": ["claude"], "options": ["--mount", "~/.config/sandbox-ai:/mnt/c:ro"]}))
    assert host.run("presets", "dirs", "add", str(where)).returncode == 0
    result = host.run("claude", "--dry-run", "--preset", "grab")
    assert result.returncode != 0, result.stdout


# --- the web interface: what a command warned reaches the page -------------------------------------------------------

def test_the_web_interface_passes_the_launchers_warnings_on():
    """`workspace add` of a folder you chose with a risk succeeds on the host and warns: the page gets the warnings
    (its toast shows them, and stays), not only the output."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("webui_mounts", ROOT / "sandbox" / "webui.py")
    web = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(web)
    said = ("agent-sandbox: warning: Workspace /home/u is your HOME directory: the agent can change your shell start "
            "files\nagent-sandbox: something else\n")

    def runner(host, args, timeout=120, input=None):
        return 0, "added /home/u to web at /workspaces/u (rw)\n", said
    answer = web.Api(runner).dispatch("POST", "/api/hosts/ct1/workspaces/web", {"path": "/home/u", "confirm": True})
    assert answer["output"].startswith("added /home/u")
    assert answer["warnings"] == ["Workspace /home/u is your HOME directory: the agent can change your shell start "
                                  "files"]
    quiet = web.Api(lambda *a, **k: (0, "added\n", "")).dispatch("POST", "/api/hosts/ct1/workspaces/web",
                                                                 {"path": "/srv/p", "confirm": True})
    assert "warnings" not in quiet
    script = (ROOT / "sandbox" / "web" / "app.js").read_text()
    assert "doneToast(t(\"Added {what} to {where}\"" in script and "r.warnings" in script
