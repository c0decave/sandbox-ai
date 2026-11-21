"""0.1.98, reported live on coding-101 (Sessions › Add MCP server… to a running session): that host had no MCP servers
yet - "it would be practical to copy MCP servers from other hosts to an active session"; and Add… of another host's
catalog entry showed one raw line, "TARGET=HOST_FOLDER[:ro|rw], one per line", refusing anything else with "Ein
Ordner ist ZIEL=HOST_ORDNER ...".

From another host… (the session's Add MCP server… and the MCP servers page): the other hosts' servers read in the
background, a check (mcp copy --dry-run) whose plan gives each folder of the source a folder field of the target
(… chooses there) or leaves it out, a missing secret kept there, OK only after a clean check of the same choices, the
copy asked first (mcp-copy) and attached to the running start. Add… of another host's entry: the whole entry from
`mcp catalog --json` there, the same fields as here; a host too old for that says so and takes a plain folder for the
one folder the entry takes. Offscreen against stand-ins that record what they were asked, and end to end through the
real ./sandbox-ai with a fake ssh to another host's real sandbox-ai."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets", reason="PySide6 is not installed")
from sandbox_ai_app import backend, widgets  # noqa: E402
from test_live_mcp_ui import HOSTS, at_once, page_on, ran, sai, ui  # noqa: E402,F401  (fixtures)

pytestmark = pytest.mark.skipif(not os.environ.get("SANDBOX_AI_HOME") and not backend.shutil.which("sandbox-ai"),
                                reason="set SANDBOX_AI_HOME to a sandbox-ai checkout")
DIGEST = "sha256:" + "a" * 64
PLAN_PATH = {"server": "own", "from_name": "own", "kind": "image", "how": "definition", "replace": False,
             "copied": ["network full"], "changes": [], "notes": [], "warnings": [],
             "secrets": {"needed": [{"variable": "TOKEN", "secret": "OWN_TOKEN", "required": True, "from": "secret"}],
                         "missing": ["OWN_TOKEN"], "known": True},
             "paths": [{"kind": "mount", "target": "/data", "path": "/home/me/data", "mode": "rw", "to": None,
                        "dropped": False}],
             "image": {"ref": f"r.example/own@{DIGEST}", "present": True, "action": "none"},
             "problems": [{"kind": "path", "text": "the folder /data ... is the source's", "fix": "--path ..."},
                          {"kind": "secret", "text": "ct1 has no secret OWN_TOKEN", "fix": "secrets set"}]}


@pytest.fixture(autouse=True)
def dialog_at_once(monkeypatch):
    """The copy dialog's reads and checks on this thread, as the pages' (test_live_mcp_ui's ui fixture)."""
    from sandbox_ai_app import mcp_copy_view
    monkeypatch.setattr(mcp_copy_view, "background", at_once)


# -- the words (no Qt) --

def test_the_copy_is_one_fixed_command_of_this_machine():
    assert backend.mcp_copy_args("lab1", "ct1", ["own"]) == ["mcp", "copy", "own", "--from", "lab1", "--to", "ct1",
                                                            "--json", "--dry-run"]
    assert backend.mcp_copy_args("local", "ct1", ["own"], as_name="own2", paths={"/home/me/data": "/srv/data"},
                                 drop_paths=["/home/me/cache"], replace=True, attach="api", dry_run=False,
                                 secrets_later=True, copy_image=True, no_vpn=True) == [
        "mcp", "copy", "own", "--from", "local", "--to", "ct1", "--json", "--as=own2", "--path=/home/me/data=/srv/data",
        "--drop-path=/home/me/cache", "--secrets-later", "--copy-image", "--no-vpn", "--attach=api", "--replace",
        "--yes"]
    for bad in (dict(source="-x", target="ct1", names=["own"]), dict(source="lab1", target="ct1", names=["--all"]),
                dict(source="ct1", target="ct1", names=["own"]),
                dict(source="lab1", target="ct1", names=["a", "b"], as_name="c"),
                dict(source="lab1", target="ct1", names=["own"], paths={"relative": "/x"}),
                dict(source="lab1", target="ct1", names=["own"], attach="--all"),
                dict(source="lab1", target="ct1", names=["own"], pull_image=True, copy_image=True),
                dict(source="lab1", target="ct1", names=["own"], everything=True)):
        with pytest.raises(ValueError):
            backend.mcp_copy_args(**bad)


def test_the_answer_of_the_copy_and_an_older_host(sai, monkeypatch):
    calls = []

    def call(*args, timeout=120, input=None, argv=None):
        calls.append(args)
        if "own" in args:
            return 1, json.dumps({"from": "lab1", "to": "ct1", "servers": [PLAN_PATH], "attached": [],
                                  "written": False, "dry_run": True}), ""
        return 1, "", ("sandbox-ai mcp: the sandbox-ai on ct1 does not know `sandbox-ai mcp import` yet: it needs "
                       "0.1.98 or later (this machine has 0.1.98); update it from here: sandbox-ai hosts update ct1 "
                       "--from-here, then copy again\n")
    monkeypatch.setattr(sai, "call", call)
    result = sai.host_mcp_copy_check("lab1", "ct1", ["own"], copy_image=True)
    assert calls[-1][-2:] == ("--copy-image", "--dry-run") and result["status"] == 1
    assert result["servers"][0]["problems"][0]["kind"] == "path"
    monkeypatch.setattr(sai, "version", lambda: "0.1.98")
    with pytest.raises(backend.HostTooOld) as refused:
        sai.host_mcp_copy("lab1", "ct1", ["web"], dry_run=False)
    assert refused.value.host == "ct1" and refused.value.command == ("mcp", "import")
    assert "sandbox-ai hosts update ct1 --from-here" in str(refused.value) and "0.1.98" in str(refused.value)


def test_the_servers_of_the_other_hosts(sai, monkeypatch):
    monkeypatch.setattr(sai, "remote_hosts", lambda: ["ct1", "lab1", "gone"])

    def listed(host):
        if host == "gone":
            raise ValueError("ssh: connect to host gone port 22: No route to host")
        return [{"name": "demo", "catalog": "built-in", "own": False}, {"name": "team-x", "own": False},
                {"name": f"own-{host}", "kind": "image", "catalog": "own (mcp.json)", "own": True}]
    monkeypatch.setattr(sai, "host_mcp_list", listed)
    found = {host: (servers, error) for host, servers, error in sai.host_mcp_elsewhere("ct1")}
    assert set(found) == {"local", "lab1", "gone"}, "every host but the target"
    assert [s["name"] for s in found["lab1"][0]] == ["own-lab1"], "built-in and teams' servers are on every host"
    assert found["gone"] == ([], "ssh: connect to host gone port 22: No route to host")


def test_another_hosts_catalog_entry_comes_whole(sai):
    """The coding-101 bug: `mcp catalog --json` there, every field the dialog shows; a host before 0.1.95 its lines."""
    sai.answers[("mcp", "catalog")] = (0, json.dumps({"entries": [
        {"entry": "hub:binwalk", "prefix": "hub", "name": "binwalk", "title": "binwalk", "description": "Firmware",
         "network": "none", "hosts": [], "published": True, "signature": "good", "catalog": "axross-mcp-hub",
         "env": [{"name": "DEPTH", "description": "how deep", "secret": False, "required": False},
                 {"name": "bad name", "description": "left out"}],
         "mounts": [{"target": "/work", "required": True, "description": "the firmware"},
                    {"target": "../etc", "required": False}],
         "limits": {"memory": "2g", "pids": 512, "evil": "x"}, "tool_timeout": 600}], "problems": []}), "")
    entries, problems = sai.host_mcp_catalog("ct1")
    assert sai.asked[-1] == ("ct1", "mcp", "catalog", "--json") and not problems
    prefix, entry = entries[0]
    assert prefix == "hub" and "_listed" not in entry
    assert entry["mounts"] == [{"target": "/work", "required": True, "description": "the firmware"}]
    assert [v["name"] for v in entry["env"]] == ["DEPTH"] and entry["limits"] == {"memory": "2g", "pids": 512}
    assert entry["_signature"] == {"state": "good"} and entry["digest"] == "published"
    # An older host: the --json is refused, its lines are read.
    sai.answers[("mcp", "catalog")] = lambda host, args: (
        (2, "", "sandbox-ai mcp: error: unrecognized arguments: --json") if "--json" in args else
        (0, "hub:binwalk  [none]  binwalk  unsigned\n", ""))
    entries, _ = sai.host_mcp_catalog("ct1")
    assert entries[0][1]["_listed"] and sai.asked[-1] == ("ct1", "mcp", "catalog")


def test_add_of_another_hosts_entry_has_the_same_fields_as_here(ui, sai):
    entry = {"name": "binwalk", "title": "binwalk", "network": "none", "_signature": {"state": "good"},
             "mounts": [{"target": "/work", "required": True, "description": "the firmware"}, {"target": "/out"}],
             "env": [{"name": "DEPTH", "description": "how deep", "secret": False, "required": False}]}
    dialog = ui.AddServerDialog(None, entry, "ct1")
    assert dialog.mount_lines is None, "no raw line"
    field, writable = dialog.mounts["/work"]
    assert field.host == "ct1" and writable.text() == "writable", "… chooses on ct1"
    assert dialog.env is not None
    assert "the firmware" in [label.text() for label in dialog.findChildren(QtWidgets.QLabel)], "what it is for"
    field.set_value("/srv/fw")
    writable.setChecked(True)
    assert dialog.mount_values() == [("/work", "/srv/fw", "rw")]
    dialog.deleteLater()


def test_an_older_host_says_so_and_takes_a_plain_folder(ui, sai):
    entry = {"name": "binwalk", "_listed": True, "_known_mounts": [{"target": "/work", "required": True}]}
    dialog = ui.AddServerDialog(None, entry, "ct1")
    notes = [label.text() for label in dialog.findChildren(QtWidgets.QLabel)]
    assert any("older than 0.1.95" in text and "/work (required)" in text for text in notes)
    dialog.mount_lines.setPlainText("/srv/fw\n/out=/srv/out:rw")
    assert dialog.mount_values() == [("/work", "/srv/fw", "ro"), ("/out", "/srv/out", "rw")]
    unknown = ui.AddServerDialog(None, {"name": "x", "_listed": True}, "ct1")
    unknown.mount_lines.setPlainText("/srv/fw")
    with pytest.raises(ValueError, match="TARGET=FOLDER, such as /work=/srv/fw"):
        unknown.mount_values()
    for made in (dialog, unknown):
        made.deleteLater()


def copy_answers(sai, monkeypatch, plans):
    """host_mcp_elsewhere and the copy's answers: PLANS, one per call (the last one again)."""
    monkeypatch.setattr(sai, "host_mcp_elsewhere", lambda target: [
        ("local", [{"name": "own", "kind": "image", "catalog": "own (mcp.json)"}], None),
        ("lab1", [], "ssh: No route to host")])
    seen = []

    def copy(source, target, names, **choices):
        seen.append((source, target, names, choices))
        plan = plans[min(len(seen) - 1, len(plans) - 1)]
        return {"from": source, "to": target, "servers": [plan], "attached": [{"server": "own", "start": "api",
                "attached": "live", "agent_restart": None, "message": "api reaches own now"}]
                if not choices.get("dry_run", True) and choices.get("attach") else [],
                "written": not choices.get("dry_run", True), "dry_run": choices.get("dry_run", True), "status": 0}
    monkeypatch.setattr(sai, "host_mcp_copy", copy)
    return seen


def test_from_another_host_into_a_running_session(ui, sai, monkeypatch, tmp_path):
    """The Add MCP server… of a running session on ct1 (no servers there): From another host…, checked, its folder
    and secret resolved, asked first, copied and attached in one command."""
    clean = {**PLAN_PATH, "problems": [], "secrets": {**PLAN_PATH["secrets"], "missing": []},
             "paths": [{**PLAN_PATH["paths"][0], "to": "/srv/data"}]}
    seen = copy_answers(sai, monkeypatch, [PLAN_PATH, clean, clean])
    monkeypatch.setattr(sai, "host_mcp_servers", lambda host: [])
    kept = []
    monkeypatch.setattr(sai, "set_secret", lambda name, value, host="local": kept.append((name, value, host))
                        or (0, "", ""))
    monkeypatch.setattr(widgets, "AUTO_SECRET", "typed-here")  # (the app's own dialog for a secret's value)
    instance = backend.Instance("session", "remote:ct1:api", "api", host="ct1", state="running", agent="claude")
    page = page_on(ui, sai, monkeypatch, instance)
    notices = []
    monkeypatch.setattr(ui, "short_notice", lambda parent, title, text, **kw: notices.append(text))
    offered = []

    def choose(dialog):
        offered.append([dialog.choice.itemText(i) for i in range(dialog.choice.count())])
        dialog.choice.setCurrentIndex(dialog.choice.findData(ui.COPY_CHOICE))
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.LiveMcpDialog, "exec", choose)
    states = []

    def resolve(dialog):
        ok = dialog.buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        assert dialog.source.currentData() == ("local", "own") and "lab1" in dialog.unread.text()
        assert not ok.isEnabled(), "nothing checked yet"
        dialog.check()
        states.append(ok.isEnabled())
        field, leave = dialog.folder_fields["/home/me/data"]
        assert field.host == "ct1", "… chooses a folder of the target"
        assert "to resolve" in dialog.plan.toPlainText()
        keep = [b for b in dialog.secrets_holder.findChildren(QtWidgets.QPushButton) if "OWN_TOKEN" in b.text()]
        keep[0].click()  # typed here, kept on ct1 only; then checked again
        field.set_value("/srv/data")
        assert not ok.isEnabled(), "the choices changed: checked again first"
        dialog.check()
        states.append(ok.isEnabled())
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.McpCopyDialog, "exec", resolve)
    page.add_mcp()
    assert "From another host…" in offered[0]
    assert kept == [("OWN_TOKEN", "typed-here", "ct1")], "the value goes to ct1 alone"
    assert states == [False, True]
    assert [action for action, *_ in ui.asked] == ["mcp-copy"]
    assert seen[-1][:3] == ("local", "ct1", ["own"])
    assert seen[-1][3]["dry_run"] is False and seen[-1][3]["attach"] == "api"
    assert seen[-1][3]["paths"] == {"/home/me/data": "/srv/data"}
    assert notices == ["api reaches own now"], "sandbox-ai's message of the attach, as it is"


def test_from_another_host_on_the_mcp_page_and_declined(ui, sai, monkeypatch):
    clean = {**PLAN_PATH, "problems": [], "paths": []}
    seen = copy_answers(sai, monkeypatch, [clean])
    page = ui.McpPage(sai)

    def accept(dialog):
        dialog.as_name.setText("own2")
        dialog.flags["replace"].setChecked(True)
        dialog.check()
        return QtWidgets.QDialog.DialogCode.Accepted
    monkeypatch.setattr(ui.McpCopyDialog, "exec", accept)
    page.copy_from_host()
    assert [action for action, *_ in ui.asked] == ["mcp-copy", "mcp-replace"]
    assert seen[-1][3]["as_name"] == "own2" and seen[-1][3]["replace"] and "attach" not in seen[-1][3]
    assert "copied" in page.catalog_status.toPlainText()
    seen.clear()
    ui.asked.clear()
    ui.answer_yes = False
    page.copy_from_host()
    assert [choices.get("dry_run") for *_, choices in seen] == [True], "only the check ran: nothing was copied"


# -- end to end: the app's backend, the real ./sandbox-ai, a fake ssh to another host's real sandbox-ai --

SSH = r'''#!PYTHON
import json, os, sys
args = sys.argv[1:]
if "-O" in args or "-G" in args or "--" not in args:
    sys.exit(255)
at = args.index("--")
host = args[at + 1].rpartition("@")[2].split(".")[0]
env = json.load(open(os.path.join(os.environ["FAKE_HOSTS"], host + ".json")))
os.chdir(env["HOME"])
os.execve("/bin/sh", ["sh", "-c", args[at + 2]], env)
'''


@pytest.fixture
def fleet(tmp_path, monkeypatch):
    """This machine (the test's XDG folders) and ct1 (its own HOME), the fake ssh between them."""
    root = Path(os.environ.get("SANDBOX_AI_HOME") or Path(backend.shutil.which("sandbox-ai")).resolve().parent)
    bin_dir, hosts_dir, there = tmp_path / "bin", tmp_path / "hosts", tmp_path / "ct1"
    for folder in (bin_dir, hosts_dir, there / ".config/sandbox-ai", there / "bin"):
        folder.mkdir(parents=True)
    (bin_dir / "ssh").write_text(SSH.replace("PYTHON", sys.executable, 1))
    (bin_dir / "ssh").chmod(0o755)
    (there / "bin/sandbox-ai").write_text(f'#!/bin/sh\nexec "{root / "sandbox-ai"}" "$@"\n')
    (there / "bin/sandbox-ai").chmod(0o755)
    env = {"HOME": str(there), "PATH": f"{there / 'bin'}:/usr/bin:/bin", "XDG_CONFIG_HOME": str(there / ".config"),
           "XDG_STATE_HOME": str(there / ".local/state"), "XDG_DATA_HOME": str(there / ".local/share"),
           "XDG_CACHE_HOME": str(there / ".cache"), "SANDBOX_AI_SECRETS": "file", "SANDBOX_AI_MCP_NO_IMAGE_CHECK": "1",
           "LC_ALL": "C.UTF-8"}
    (hosts_dir / "ct1.json").write_text(json.dumps(env))
    for name, sub in (("XDG_CONFIG_HOME", "config"), ("XDG_STATE_HOME", "state"), ("XDG_DATA_HOME", "data"),
                      ("XDG_CACHE_HOME", "cache"), ("XDG_RUNTIME_DIR", "runtime")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    (tmp_path / "runtime").mkdir(mode=0o700)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_HOSTS", str(hosts_dir))
    monkeypatch.setenv("SANDBOX_AI_SSH_MULTIPLEX", "0")
    monkeypatch.setenv("SANDBOX_AI_SECRETS", "file")
    config = tmp_path / "config/sandbox-ai"
    config.mkdir(parents=True)
    (config / "hosts.json").write_text(json.dumps({"version": 1, "hosts": {"ct1": {"ssh": "me@ct1.example",
                                                                                  "port": 22}}}))
    return {"there": there, "here": tmp_path, "config": config}


def test_e2e_the_entry_of_another_hosts_catalog_and_a_copy_to_it(fleet, tmp_path):
    sai = backend.SandboxAI()
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema": "axross-mcp-catalog", "version": 1, "entries": [
        {"id": "hub:binwalk", "name": "binwalk", "title": "binwalk", "kind": "oci", "description": "Firmware",
         "image": "ghcr.io/c0decave/axross-mcp-binwalk", "digest": DIGEST, "network": "none",
         "mounts": [{"target": "/work", "required": True, "description": "the firmware"}],
         "limits": {"memory": "1g"}}]}))
    entries, problems = sai.host_mcp_catalog("ct1", [str(catalog)])
    assert not problems, problems
    prefix, entry = entries[0]
    assert entry["mounts"] == [{"target": "/work", "required": True, "description": "the firmware"}]
    assert entry["limits"] == {"memory": "1g"} and "_listed" not in entry
    # A server of this machine copied to ct1: its folder resolved, and nothing of this machine taken unasked.
    data = tmp_path / "data"
    data.mkdir()
    (fleet["config"] / "mcp.json").write_text(json.dumps({"own": {
        "image": f"registry.example.org/me/own@{DIGEST}", "mounts": [{"target": "/data", "host": str(data),
                                                                      "mode": "ro"}]}}))
    result = sai.host_mcp_copy_check("local", "ct1", ["own"])
    assert result["status"] == 1 and result["servers"][0]["problems"][0]["kind"] == "path"
    there_data = fleet["there"] / "data"
    there_data.mkdir()
    result = sai.host_mcp_copy("local", "ct1", ["own"], paths={str(data): str(there_data)}, dry_run=False)
    assert result["written"], result
    defined = json.loads((fleet["there"] / ".config/sandbox-ai/mcp.json").read_text())["own"]
    assert defined["mounts"] == [{"target": "/data", "host": str(there_data), "mode": "ro"}]
