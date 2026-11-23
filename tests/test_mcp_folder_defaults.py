"""MCP servers' folder defaults (user 2026-11-22: "alle mcp server sollten bzgl. /work pfad defaults haben, sodass sie
simpel und fast hinzugefügt werden können"; "--mount /work=/app:rw ... es geht doch um container und nicht die
maschine???").

`sandbox-ai mcp add NAME` works for every catalog server without --mount: the container's /work is by default the
workspace of the start that uses the server (the marker "@workspace" in the definition, bound at each start, in an
instance of that workspace's own - never shared with another project's start), every other folder the server declares
a folder of its own under sandbox-ai's data directory (made 0700 at add, never through a symlink). An explicit --mount
wins; `mcp set NAME --mount TARGET=default` goes back to the default.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / "sandbox" / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


s = load("agent_sandbox_folder_defaults_test", "agent_sandbox.py")
m = load("sandbox_mcp_folder_defaults_test", "mcp.py")
DIGEST = "sha256:" + "b" * 64

BINWALK = {"id": "hub:binwalk", "name": "binwalk", "title": "binwalk", "kind": "oci",
           "image": "ghcr.io/c0decave/axross-mcp-binwalk", "digest": DIGEST, "network": "none",
           "mounts": [{"target": "/work", "required": True,
                       "description": "The folder with the firmware images (read-only is enough)"},
                      {"target": "/out", "description": "A folder for extracted files (tick writable)"}]}
EXEC = {"id": "mcp:exec", "name": "exec", "title": "exec", "kind": "oci", "image": "ghcr.io/c0decave/sandbox-ai-mcp-exec",
        "digest": DIGEST, "network": "none",
        "mounts": [{"target": "/work", "required": True,
                    "description": "The project folder, writable (--mount /work=PROJECT:rw): the agent's workspace "
                                   "folder itself"}]}
AXROSS = {"id": "mcp:axross", "name": "axross", "title": "axross", "kind": "oci",
          "image": "ghcr.io/c0decave/sandbox-ai-mcp-axross", "digest": DIGEST, "network": "none",
          "mounts": [{"target": "/work", "required": False,
                      "description": "The folder the file tools work on, read-only (--mount /work=DIR:ro; :rw with "
                                     "AXROSS_WRITE=1 to change it); without it an empty folder"},
                     {"target": "/ssh", "description": "known_hosts, read-only (--mount /ssh=~/.ssh:ro)"}]}
TELEGRAM = {"id": "mcp:telegram-user", "name": "telegram-user", "title": "Telegram", "kind": "oci",
            "image": "ghcr.io/c0decave/sandbox-ai-mcp-telegram-user", "digest": DIGEST, "network": "none",
            "description": "media as metadata, downloaded only into a mounted writable /out (size-capped)",
            "mounts": [{"target": "/out"}]}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    for variable, folder in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "cfg"), ("XDG_DATA_HOME", "data"),
                             ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(variable, str(tmp_path / folder))
    monkeypatch.setattr(m, "PROC", tmp_path / "proc")
    (tmp_path / "proc").mkdir()


def catalog(tmp_path, entries):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": entries}))
    return str(path)


def own(tmp_path, name, folder):
    return tmp_path / "data" / "sandbox-ai" / "mcp-folders" / name / folder


def add(tmp_path, capsys, *args):
    """`sandbox-ai mcp add ARGS` through the CLI's main: (status, printed)."""
    status = m.cli(["add", *args, "--catalog", catalog(tmp_path, [BINWALK, EXEC, AXROSS, TELEGRAM]), "--no-pull",
                    "--unsigned"])
    printed = capsys.readouterr()
    return status, " ".join((printed.out + printed.err).split())


def mounts_of(name):
    return {mount["target"]: mount for mount in json.loads(m.config_file().read_text())[name]["mounts"]}


def test_add_without_mount_gives_every_folder_its_default(tmp_path, capsys):
    """binwalk (requires /work, takes /out): added without --mount - /work is the start's workspace (read-only, as its
    entry says), /out a folder of its own, made 0700 and named in what add prints."""
    status, said = add(tmp_path, capsys, "binwalk")
    assert status == 0, said
    folders = mounts_of("binwalk")
    assert folders["/work"] == {"target": "/work", "host": "@workspace", "mode": "ro"}
    out = own(tmp_path, "binwalk", "out")
    assert folders["/out"] == {"target": "/out", "host": str(out), "mode": "rw"}
    assert out.is_dir() and out.stat().st_mode & 0o777 == 0o700
    assert out.parent.stat().st_mode & 0o777 == 0o700
    assert "/work: the start's workspace (read-only)" in said
    assert f"/out: its own folder {out} (writable)" in said
    assert "sandbox-ai's own folder" not in said, "its own folder is no risk you chose: no warning"


def test_the_mode_of_a_default_is_what_the_entry_declares(tmp_path, capsys):
    """Read-only unless the entry says the server writes there: exec's "writable (--mount /work=PROJECT:rw)", axross's
    "read-only (--mount /work=DIR:ro; :rw with ...)", telegram-user's "downloaded only into a mounted writable /out"."""
    for name in ("exec", "axross", "telegram-user"):
        status, said = add(tmp_path, capsys, name)
        assert status == 0, said
    assert mounts_of("exec")["/work"] == {"target": "/work", "host": "@workspace", "mode": "rw"}
    assert mounts_of("axross")["/work"]["mode"] == "ro" and mounts_of("axross")["/ssh"]["mode"] == "ro"
    assert mounts_of("axross")["/ssh"]["host"] == str(own(tmp_path, "axross", "ssh"))
    assert own(tmp_path, "axross", "ssh").is_dir(), "a read-only folder of its own is made too (empty)"
    assert mounts_of("telegram-user")["/out"]["mode"] == "rw"


def test_an_explicit_mount_wins_and_default_names_the_default(tmp_path, capsys):
    """--mount TARGET=HOST still decides that folder; --mount TARGET=default (with :ro or :rw) is the default; a name
    given with --as gets its own folders under that name."""
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    status, said = add(tmp_path, capsys, "binwalk", "--mount", f"/work={firmware}", "--mount", "/out=default")
    assert status == 0, said
    assert mounts_of("binwalk")["/work"] == {"target": "/work", "host": str(firmware), "mode": "ro"}
    assert mounts_of("binwalk")["/out"]["host"] == str(own(tmp_path, "binwalk", "out"))
    status, said = add(tmp_path, capsys, "binwalk", "--as", "fw2", "--mount", "/work=default:rw")
    assert status == 0, said
    assert mounts_of("fw2")["/work"] == {"target": "/work", "host": "@workspace", "mode": "rw"}
    assert mounts_of("fw2")["/out"]["host"] == str(own(tmp_path, "fw2", "out"))


def test_set_goes_back_to_the_default(tmp_path, capsys):
    """`mcp set NAME --mount /work=default`: the start's workspace again; a folder left out of set's --mount gets its
    default (not dropped)."""
    firmware = tmp_path / "firmware"
    firmware.mkdir()
    status, said = add(tmp_path, capsys, "binwalk", "--mount", f"/work={firmware}")
    assert status == 0, said
    assert m.cli(["set", "binwalk", "--mount", "/work=default"]) == 0, "through the CLI: its catalog is the file"
    assert mounts_of("binwalk")["/work"] == {"target": "/work", "host": "@workspace", "mode": "ro"}
    assert mounts_of("binwalk")["/out"]["host"] == str(own(tmp_path, "binwalk", "out"))
    out = tmp_path / "kept"
    assert m.cli(["set", "binwalk", "--mount", f"/out={out}:rw"]) == 0
    assert mounts_of("binwalk")["/work"]["host"] == "@workspace", "left out of --mount: its default"
    assert mounts_of("binwalk")["/out"]["host"] == str(out)
    capsys.readouterr()


def test_its_own_folder_is_never_made_through_a_symlink(tmp_path, capsys):
    """A link planted where the server's own folders go (mcp-folders/NAME -> elsewhere): refused, nothing is made
    behind it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    base = tmp_path / "data" / "sandbox-ai" / "mcp-folders"
    base.mkdir(parents=True)
    (base / "binwalk").symlink_to(elsewhere)
    with pytest.raises(ValueError, match="symbolic link or no folder"):
        add(tmp_path, capsys, "binwalk")
    assert list(elsewhere.iterdir()) == []
    assert not m.config_file().exists() or "binwalk" not in json.loads(m.config_file().read_text())


def test_catalog_json_says_each_folders_default(tmp_path):
    """`mcp catalog --json` (the app's and the web's add forms, another host's too): each folder's default and mode."""
    entry = {**BINWALK, "_signature": {"state": "unsigned"}}
    mounts = {mount["target"]: mount for mount in m.catalog_json("hub", entry)["mounts"]}
    assert (mounts["/work"]["default"], mounts["/work"]["mode"]) == ("workspace", "ro")
    assert (mounts["/out"]["default"], mounts["/out"]["mode"]) == ("own", "rw")


def test_show_names_the_start_s_workspace_and_its_own_folder(tmp_path, capsys):
    status, said = add(tmp_path, capsys, "binwalk")
    assert status == 0, said
    assert m.cli(["show", "binwalk"]) == 0
    shown = " ".join(capsys.readouterr().out.split())
    assert "/work: the start's workspace (read-only)" in shown
    assert f"/out: its own folder {own(tmp_path, 'binwalk', 'out')} (writable)" in shown
    assert m.cli(["show", "binwalk", "--json"]) == 0
    folders = {f["target"]: f for f in json.loads(capsys.readouterr().out)["folders"]}
    assert folders["/work"]["default"] == "workspace" and folders["/out"]["default"] == "own"
    with pytest.raises(ValueError, match="no MCP server nothere"):
        m.cli(["show", "nothere"])


# --- the start's workspace: bound at each start, one instance per workspace -----------------------------------------

def workspace_server(mode="ro"):
    config = m.config_file()
    config.parent.mkdir(parents=True, exist_ok=True)
    definition = {"image": f"ghcr.io/a/radare2@{DIGEST}", "network": "none",
                  "mounts": [{"target": "/work", "host": "@workspace", "mode": mode}]}
    config.write_text(json.dumps({"r2": definition}))
    return definition


def start(tmp_path, folder, *more):
    workspace = tmp_path / folder
    workspace.mkdir(exist_ok=True)
    a = s.parse_args(["codex", "--mcp", "r2", "--workspace", str(workspace), *more])
    a.workspaces = [str(workspace.resolve())]  # (as validate resolves them)
    return a, workspace.resolve()


def test_a_workspace_bound_server_runs_per_workspace(tmp_path, monkeypatch):
    """Two starts of two projects never share it: each gets the instance of its own workspace, which mounts that
    workspace (read-only as defined); two starts of the same workspace share theirs; the server's own name NAME never
    runs bound to anything."""
    workspace_server()
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    monkeypatch.setattr(m, "verified_or_stopped", lambda *a, **kw: None)
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: None)
    one, first = start(tmp_path, "project-one")
    two, second = start(tmp_path, "project-two")
    again, _ = start(tmp_path, "project-one")
    i1, i2, i3 = m.instance_of(one, "r2"), m.instance_of(two, "r2"), m.instance_of(again, "r2")
    assert i1 != "r2" and i2 != "r2" and i1 != i2 and i1 == i3
    assert m.start_problems(one, ["r2"], present=lambda image: True)[1] == {"r2": i1}
    assert m.ensure_running("r2", one) == "started"
    assert f"--volume={first}:/work:ro" in " ".join(started[-1])
    assert m.ensure_running("r2", two) == "started"
    assert f"--volume={second}:/work:ro" in " ".join(started[-1])
    assert str(first) not in " ".join(started[-1]), "another project's workspace never reaches this start's server"
    assert m.status("r2") != "running", "NAME itself is bound to no workspace: it never runs"
    assert m.load_state(i1)["mounts"] == [{"target": "/work", "host": str(first), "mode": "ro"}]
    assert m.load_state(i2)["mounts"] == [{"target": "/work", "host": str(second), "mode": "ro"}]


def test_a_workspace_bound_server_s_own_folders_are_per_workspace_too(tmp_path, monkeypatch, capsys):
    """Security review 2026-11-22: binwalk (/work the start's workspace, /out a folder of its own, writable) ran one
    instance per project, but each instance mounted the same own folder /out - what the server extracted from one
    project's firmware was there for another project's agent (and what that agent put there for the first one). A
    server bound to the start's workspace keeps each of its own folders per workspace too; one never mounts
    another's, and never the folder all of them are below."""
    status, said = add(tmp_path, capsys, "binwalk")
    assert status == 0, said
    started = []

    class Process:
        pid = os.getpid()

        def poll(self):
            return None
    monkeypatch.setattr(m.subprocess, "Popen", lambda cmd, **kw: started.append(cmd) or Process())
    monkeypatch.setattr(m, "wait_for_socket", lambda *a, **kw: None)
    monkeypatch.setattr(m, "verified_or_stopped", lambda *a, **kw: None)
    monkeypatch.setattr(m.launcher(), "delegated_controllers", lambda: None)
    outs = []
    for folder in ("project-one", "project-two"):
        workspace = tmp_path / folder
        workspace.mkdir()
        a = s.parse_args(["codex", "--mcp", "binwalk", "--workspace", str(workspace)])
        a.workspaces = [str(workspace.resolve())]
        assert m.ensure_running("binwalk", a) == "started"
        volumes = [word.split("=", 1)[1] for word in started[-1] if word.startswith("--volume=")]
        out = [volume.rsplit(":", 2)[0] for volume in volumes if volume.rsplit(":", 2)[1] == "/out"]
        assert len(out) == 1, volumes
        outs.append(Path(out[0]))
    shared = own(tmp_path, "binwalk", "out")
    assert outs[0] != outs[1], "two projects' instances write the same /out"
    for out in outs:
        assert out != shared and shared not in out.parents and out.is_dir() and not out.is_symlink()
        assert out.stat().st_mode & 0o777 == 0o700
        assert str(own(tmp_path, "binwalk", "")) in str(out), "still the server's own folder, below mcp-folders"


def test_a_start_s_own_mcp_mount_still_wins_and_never_widens(tmp_path):
    workspace_server()
    elsewhere = tmp_path / "samples"
    elsewhere.mkdir()
    a, _ = start(tmp_path, "project", "--mcp-mount", f"r2:/work={elsewhere}")
    definition = m.start_definition("r2", a)[1]
    assert definition["mounts"] == [{"target": "/work", "host": str(elsewhere), "mode": "ro"}]
    b, _ = start(tmp_path, "project", "--mcp-mount", f"r2:/work={elsewhere}:rw")
    with pytest.raises(ValueError, match="read-only"):
        m.start_definition("r2", b)


def test_without_a_start_s_workspace_it_is_refused_never_run_unbound(tmp_path):
    """`mcp start NAME` on its own has no start's workspace: refused with how to start it (--for START, a start with
    --mcp NAME, or a folder of its own) - never started with the marker or the folder it was run in."""
    workspace_server()
    with pytest.raises(ValueError) as caught:
        m.cli(["start", "r2"])
    said = " ".join(str(caught.value).split())
    assert "the workspace of the start that uses it" in said and "--for START" in said
    assert "mcp set r2 --mount /work=FOLDER" in said
    with pytest.raises(ValueError, match="start's workspace"):
        m.image_command(m.server_network("r2", m.definitions()["r2"]))


def test_the_launcher_mounts_the_instance_of_its_workspace(tmp_path, monkeypatch, capsys):
    """The launcher's dry run (what a start would do): the agent reaches the instance of its own workspace."""
    workspace_server()
    monkeypatch.setattr(s, "load_mcp_module", lambda: m)
    monkeypatch.setenv("SANDBOX_AI_MCP_NO_IMAGE_CHECK", "1")
    workspace = tmp_path / "project"
    workspace.mkdir()
    s.main(["codex", "--mcp", "r2", "--workspace", str(workspace), "--workspaces-as-given", "--dry-run"])
    plan = json.loads(capsys.readouterr().out)["mcp"]
    a, _ = start(tmp_path, "project")
    assert plan[0]["instance"] == m.instance_of(a, "r2") != "r2"


def test_mcp_add_through_sandbox_ai_needs_no_mount(tmp_path):
    """End to end: `./sandbox-ai mcp add binwalk` with a catalog file and no --mount is added; `mcp show` says where
    its folders are."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    path = catalog(tmp_path, [BINWALK])
    added = host.run("mcp", "add", "binwalk", "--catalog", path, "--no-pull", "--unsigned")
    assert added.returncode == 0, added.stderr
    text = " ".join((added.stdout + added.stderr).split())
    folder = host.home / ".local/share/sandbox-ai/mcp-folders/binwalk/out"
    assert "/work: the start's workspace (read-only)" in text and str(folder) in text
    assert folder.is_dir()
    shown = host.run("mcp", "show", "binwalk")
    assert shown.returncode == 0 and "the start's workspace" in shown.stdout, shown.stderr


def test_the_marker_is_no_folder_of_this_machine_for_the_launchers_checks(tmp_path, monkeypatch):
    """The launcher's checks of the folders image servers mount (a workspace that holds one is warned about, `workspace
    add` refuses one): the start's workspace is no folder there - never resolved against the folder the launcher runs
    in (./@workspace inside the project)."""
    workspace_server()
    monkeypatch.chdir(tmp_path)
    hosts = s.image_mount_hosts(None)
    assert hosts == [], hosts


def test_two_projects_dry_runs_through_sandbox_ai_reach_two_instances(tmp_path):
    """End to end through ./sandbox-ai with the fake Podman: binwalk added without --mount; the dry runs of two
    projects' starts reach an instance each (that project's own), two of the same project the same one."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    added = host.run("mcp", "add", "binwalk", "--catalog", catalog(tmp_path, [BINWALK]), "--no-pull", "--unsigned")
    assert added.returncode == 0, added.stderr
    host.add_image(f"ghcr.io/c0decave/axross-mcp-binwalk@{DIGEST}", "localhost/codex-sandbox:latest")
    found = []
    for name in ("one", "two", "one"):
        project = tmp_path / name
        project.mkdir(exist_ok=True)
        run = host.run("codex", "--mcp", "binwalk", "--workspace", str(project), "--workspaces-as-given", "--dry-run")
        assert run.returncode == 0, run.stderr
        found.append(json.loads(run.stdout)["mcp"][0]["instance"])
    assert found[0] != "binwalk" and found[0] != found[1] and found[0] == found[2], found


def test_a_start_says_nothing_of_sandbox_ais_own_folder_about_the_workspaces_own_folder(tmp_path):
    """Journeys round 1: binwalk added without --mount, `sandbox-ai codex --mcp binwalk` warned at every start that
    /out (its own folder, made by sandbox-ai for that workspace: .workspaces/KEY/out) "lies in ... sandbox-ai's own
    folder: the agent can change sandbox-ai itself" - of a folder nobody chose. A folder chosen there still is."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    added = host.run("mcp", "add", "binwalk", "--catalog", catalog(tmp_path, [BINWALK]), "--no-pull", "--unsigned")
    assert added.returncode == 0, added.stderr
    host.add_image(f"ghcr.io/c0decave/axross-mcp-binwalk@{DIGEST}", "localhost/codex-sandbox:latest")
    run = host.run("codex", "--mcp", "binwalk", "--dry-run")
    assert run.returncode == 0, run.stderr
    assert "sandbox-ai's own folder" not in run.stderr, run.stderr
    # a folder of sandbox-ai's own that the human chose is still said
    chosen = host.home / ".local/share/sandbox-ai/chosen"
    chosen.mkdir(parents=True)
    assert host.run("mcp", "set", "binwalk", "--mount", f"/out={chosen}:rw").returncode == 0
    run = host.run("codex", "--mcp", "binwalk", "--dry-run")
    assert "sandbox-ai's own folder" in run.stderr, run.stderr


def test_a_name_in_two_catalogs_says_how_to_add_each(tmp_path):
    """Journeys round 1: `sandbox-ai mcp add binwalk` (in sandbox-ai-mcp and in axross-mcp-hub) said only "binwalk is
    in several catalogs: mcp:binwalk, hub:binwalk" - the command that adds one of them, and which catalog each is
    of, are said now."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    run = host.run("mcp", "add", "binwalk")
    assert run.returncode != 0
    text = " ".join(run.stderr.split())
    assert "binwalk is in several catalogs" in text, text
    assert "sandbox-ai mcp add mcp:binwalk" in text and "sandbox-ai mcp add hub:binwalk" in text, text
    assert "sandbox-ai-mcp" in text and "axross-mcp-hub" in text, text


def test_attach_live_says_how_each_agent_sees_the_server_as_it_now_does(tmp_path):
    """Journeys round 1: `mcp attach --help` said --live reaches "Codex and OpenCode: after a restart of the agent",
    and README's table told Codex to `resume START --restart` (the restart that stops the sandbox) - since 0.1.214
    Codex is restarted inside its running sandbox (kits restart-agent START), and OpenCode sees one only with a new
    start. The help, README and README_DE say so."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    shown = " ".join(Host(tmp_path / "h").run("mcp", "attach", "--help").stdout.split())
    assert "Codex: after sandbox-ai kits restart-agent START" in shown, shown
    assert "Codex and OpenCode: after a restart of the agent" not in shown, shown
    for readme, codex in (("README.md", "| Codex | the server's words for the command line of its restart inside "),
                          ("README_DE.md", "| Codex | die Wörter des Servers für die Kommandozeile seines Neustarts ")):
        text = (ROOT / readme).read_text()
        row = next(line for line in text.splitlines() if line.startswith(codex))
        assert "sandbox-ai kits restart-agent START" in row, row


def test_a_link_in_place_of_the_workspaces_own_folder_is_still_said(tmp_path):
    """The exemption of the workspace's own folder (journeys round 1) is for the folder as sandbox-ai makes it: a
    symbolic link put there in its place, to a folder of sandbox-ai's own, is said as that folder is."""
    sys.path.insert(0, str(ROOT / "tests"))
    from live_fakes import Host
    host = Host(tmp_path / "h")
    added = host.run("mcp", "add", "binwalk", "--catalog", catalog(tmp_path, [BINWALK]), "--no-pull", "--unsigned")
    assert added.returncode == 0, added.stderr
    host.add_image(f"ghcr.io/c0decave/axross-mcp-binwalk@{DIGEST}", "localhost/codex-sandbox:latest")
    host.run("codex", "--mcp", "binwalk")  # (a start makes the folder; the fake Podman stops it at its pod)
    host.run("mcp", "stop", "binwalk")
    (made,) = (host.home / ".local/share/sandbox-ai/mcp-folders/binwalk/.workspaces").glob("*/out")
    elsewhere = host.home / ".local/share/sandbox-ai/approvals-like"
    elsewhere.mkdir()
    made.rmdir()
    made.symlink_to(elsewhere)
    run = host.run("codex", "--mcp", "binwalk", "--dry-run")
    assert "sandbox-ai's own folder" in run.stderr or run.returncode != 0, run.stderr
