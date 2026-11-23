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
        assert ok.isEnabled(), "nothing checked yet: Copy checks first (it was greyed out: 'immer ausgegraut')"
        dialog.check()
        states.append(ok.isEnabled())
        field, leave = dialog.folder_fields["/home/me/data"]
        assert field.host == "ct1", "… chooses a folder of the target"
        assert "to resolve" in dialog.plan.toPlainText()
        keep = [b for b in dialog.secrets_holder.findChildren(QtWidgets.QPushButton) if "OWN_TOKEN" in b.text()]
        keep[0].click()  # typed here, kept on ct1 only; then checked again
        field.set_value("/srv/data")
        assert ok.isEnabled() and dialog.checked_with is None, "the choices changed: Copy checks them again first"
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


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_dropped_folder_is_the_servers_default_folder_there(ui, sai, monkeypatch, language):
    """(2026-11-22) A folder of the source dropped is a path of that machine that means nothing on the target: the
    server gets its default folder there (sandbox-ai mcp copy --drop-path). The box says so, and the plan says
    "the default folder on HOST" - never "left out" while a folder is mounted there after all."""
    from sandbox_ai_app import i18n, mcp_copy_view
    dropped = {**PLAN_PATH, "problems": [], "secrets": {**PLAN_PATH["secrets"], "missing": []},
               "paths": [{**PLAN_PATH["paths"][0], "to": "default", "dropped": True}]}
    seen = copy_answers(sai, monkeypatch, [PLAN_PATH, dropped])
    i18n.set_language(language)
    try:
        words = {"box": "its default folder there", "said": "folder /home/me/data: the default folder on ct1"} \
            if language == "en" else {"box": "sein Standardordner dort",
                                      "said": "Ordner /home/me/data: der Standardordner auf ct1"}
        dialog = ui.McpCopyDialog(None, sai, target="ct1")
        try:
            dialog.check()
            _field, box = dialog.folder_fields["/home/me/data"]
            assert box.text() == words["box"] and "/work" in box.toolTip()
            box.setChecked(True)
            dialog.check()
            assert seen[-1][3]["drop_paths"] == ["/home/me/data"]
            assert words["said"] in dialog.plan.toPlainText(), dialog.plan.toPlainText()
        finally:
            dialog.close()
        lines = mcp_copy_view.plan_lines({"to": "ct1", "servers": [
            {**dropped, "kind": "command", "paths": [{"kind": "option", "path": "/etc", "dropped": True}]}]})
        assert any(line.strip() == ("folder /etc: left out" if language == "en" else "Ordner /etc: weggelassen")
                   for line in lines), lines
    finally:
        i18n.set_language("en")


@pytest.mark.parametrize("language", ["en", "de"])
def test_folder_defaults_of_the_source_are_said_and_ticked_as_defaults(ui, sai, monkeypatch, language):
    """Journeys round 1: a server added without --mount (/work: the start's workspace, /out: its own folder) - the
    check's plan said "folder @workspace: @workspace" and ".../mcp-folders/binwalk/out: default", and the dialog's
    folder fields held the words "@workspace" and "default" as if they were folders of the target. They are the
    server's defaults there: said so, the box "its default folder there" ticked, the field empty."""
    from sandbox_ai_app import i18n, mcp_copy_view
    own = "/home/me/.local/share/sandbox-ai/mcp-folders/binwalk/out"
    plan = {**PLAN_PATH, "problems": [], "secrets": {**PLAN_PATH["secrets"], "missing": []}, "paths": [
        {"kind": "mount", "target": "/work", "path": "@workspace", "mode": "ro", "to": "@workspace", "dropped": False},
        {"kind": "mount", "target": "/out", "path": own, "mode": "rw", "to": "default", "dropped": False}]}
    copy_answers(sai, monkeypatch, [plan])
    i18n.set_language(language)
    try:
        dialog = ui.McpCopyDialog(None, sai, target="ct1")
        try:
            dialog.check()
            text = dialog.plan.toPlainText()
            assert "@workspace" not in text and ": default" not in text, text
            if language == "en":
                assert "folder the start's workspace: the workspace of the start that uses it on ct1" in text, text
                assert f"folder {own}: a folder of its own on ct1, new and empty" in text, text
            else:
                assert "Ordner der Arbeitsordner des Starts: der Arbeitsordner des Starts, der ihn nutzt, auf ct1" \
                    in text, text
                assert f"Ordner {own}: ein eigener Ordner auf ct1, neu und leer" in text, text
            for old in ("@workspace", own):
                field, box = dialog.folder_fields[old]
                assert field.value() == "" and box.isChecked(), (old, field.value())
        finally:
            dialog.close()
    finally:
        i18n.set_language("en")


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


# -- 0.1.212, reported live: "mcpserver kopieren auf anderen host funktioniert nicht, prüfen funktioniert manchmal
# noch, aber kopieren ist immer ausgegraut"; "es sollte, wenn man einen MCP markiert hat, dann heißen: zu einem
# anderen host". The page only had From another host…: it lists the servers of the OTHER hosts (never the one
# selected), its Copy stayed greyed until a clean check of the very same choices (a folder typed after a check: greyed
# again), and a check's late answer was shown for the server chosen since. --

OK = QtWidgets.QDialogButtonBox.StandardButton.Ok
CLEAN_FETCH = {**PLAN_PATH, "server": "fetch", "from_name": "fetch", "problems": [], "paths": [],
               "secrets": {"needed": [], "missing": [], "known": True}}


def copy_to_entry(page):
    """The right-click menu's entry of Copy … to another host…, for the row selected."""
    menu = widgets.menu_of(page.servers)
    return next(action for action in menu.actions() if "another host" in action.text().replace("&", "")
                and action.text().replace("&", "").startswith("Copy"))


def test_the_selected_server_is_copied_to_another_host(ui, sai, monkeypatch):
    seen = copy_answers(sai, monkeypatch, [CLEAN_FETCH])
    page = ui.McpPage(sai)
    button = page.copy_to_button
    assert button.text() == "Copy to another host…" and not button.isEnabled()
    assert "Select a server" in button.toolTip(), "greyed out, saying why"
    page.servers.selectRow(0)
    assert button.isEnabled() and button.text() == "Copy fetch to another host…", "the action names the server"
    entry = copy_to_entry(page)
    assert entry.isEnabled() and entry.text().replace("&", "") == "Copy fetch to another host…", "on right-click too"
    page.servers.selectAll()
    assert not button.isEnabled(), "one server at a time"
    page.servers.clearSelection()
    page.servers.selectRow(1)
    assert button.text() == "Copy web to another host…"
    page.servers.selectRow(0)
    offered = []

    def accept(dialog):
        offered.append([dialog.target_choice.itemData(i) for i in range(dialog.target_choice.count())])
        assert dialog.chosen() == ("local", "fetch") and dialog.target == "ct1"
        assert dialog.buttons.button(OK).isEnabled(), "Copy checks first: never greyed out while a host is chosen"
        dialog.buttons.button(OK).click()  # the check runs, clean: accepted
        assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
        return dialog.result()
    monkeypatch.setattr(ui.McpCopyDialog, "exec", accept)
    page.copy_to_host()
    assert offered == [["ct1"]], "every host but the server's own"
    assert [dry for *_, choices in seen for dry in [choices.get("dry_run")]] == [True, False], "checked, then copied"
    assert seen[-1][:3] == ("local", "ct1", ["fetch"])
    assert [action for action, *_ in ui.asked] == ["mcp-copy"]
    assert "ct1" in page.catalog_status.toPlainText() and "fetch" in page.catalog_status.toPlainText()


def test_copy_to_another_host_in_german(ui, sai, monkeypatch):
    from sandbox_ai_app import i18n
    i18n.set_language("de")
    try:
        page = ui.McpPage(sai)
        assert page.copy_to_button.text() == "Auf einen anderen Host kopieren…"
        page.servers.selectRow(0)
        assert page.copy_to_button.text() == "fetch auf einen anderen Host kopieren…"
    finally:
        i18n.set_language("en")


def test_copy_checks_first_and_is_greyed_only_by_what_a_check_found(ui, sai, monkeypatch):
    """From another host… with a folder to resolve: Copy runs the check; greyed only while the very choices checked
    are blocked (its tooltip says why); a folder typed since makes Copy check again and copy."""
    clean = {**PLAN_PATH, "problems": [], "secrets": {**PLAN_PATH["secrets"], "missing": []},
             "paths": [{**PLAN_PATH["paths"][0], "to": "/srv/data"}]}
    seen = copy_answers(sai, monkeypatch, [PLAN_PATH, clean])
    dialog = ui.McpCopyDialog(None, sai, "ct1")
    ok = dialog.buttons.button(OK)
    assert dialog.chosen() == ("local", "own") and ok.isEnabled()
    ok.click()
    assert dialog.result() != QtWidgets.QDialog.DialogCode.Accepted, "the check found what to resolve: not copied"
    assert not ok.isEnabled() and "resolve" in ok.toolTip().lower()
    field, _leave = dialog.folder_fields["/home/me/data"]
    field.set_value("/srv/data")
    assert ok.isEnabled(), "the choices changed: Copy checks them"
    ok.click()
    assert dialog.result() == QtWidgets.QDialog.DialogCode.Accepted
    assert seen[-1][3]["paths"] == {"/home/me/data": "/srv/data"} and seen[-1][3]["dry_run"] is True
    dialog.deleteLater()


def test_a_late_check_never_answers_for_the_server_chosen_since(ui, sai, monkeypatch):
    """The race behind "prüfen funktioniert manchmal": a check's answer that comes after another server was chosen
    (or another check was started) put the old server's plan and folder fields up; a folder typed there then made the
    next check fail ("the source names no folder ...")."""
    from sandbox_ai_app import mcp_copy_view
    monkeypatch.setattr(sai, "host_mcp_elsewhere", lambda target: [
        ("local", [{"name": "own", "kind": "image", "catalog": "own (mcp.json)"},
                   {"name": "other", "kind": "image", "catalog": "own (mcp.json)"}], None)])
    pending = []

    def later(function, *args, done, **kw):
        if args:  # the other hosts' servers (host_mcp_elsewhere, TARGET): at once
            return at_once(function, *args, done=done)
        pending.append((function, done))
    monkeypatch.setattr(mcp_copy_view, "background", later)
    monkeypatch.setattr(sai, "host_mcp_copy", lambda source, target, names, **choices: {
        "from": source, "to": target, "servers": [{**PLAN_PATH, "server": names[0]}], "attached": [],
        "written": False, "dry_run": True, "status": 1})
    dialog = ui.McpCopyDialog(None, sai, "ct1")
    dialog.check()  # own
    dialog.source.setCurrentIndex(1)
    assert dialog.chosen() == ("local", "other")
    function, done = pending.pop(0)
    done(function(), None)  # own's answer, late
    assert dialog.folder_fields == {} and dialog.plan.toPlainText() == "", "own's plan is not other's"
    dialog.check()
    function, done = pending.pop(0)
    done(function(), None)
    assert "other" in dialog.plan.toPlainText() and dialog.folder_fields
    dialog.deleteLater()


def test_e2e_the_page_copies_its_server_to_another_host(fleet, tmp_path, qapp, monkeypatch):
    """The real ./sandbox-ai of this machine and of ct1 (fake ssh): Copy own to another host… of the MCP servers page
    of this machine, checked (its folder resolved in the dialog) and copied to ct1."""
    from sandbox_ai_app import mcp_copy_view, ui as module
    monkeypatch.setattr(widgets, "AUTO_ANSWER", True)
    monkeypatch.setattr(module, "ask_first", lambda *a, **k: True)
    for target in (module, mcp_copy_view):
        monkeypatch.setattr(target, "background", at_once)
    data, there_data = tmp_path / "data", fleet["there"] / "data"
    data.mkdir()
    there_data.mkdir()
    (fleet["config"] / "mcp.json").write_text(json.dumps({"own": {
        "image": f"registry.example.org/me/own@{DIGEST}", "mounts": [{"target": "/data", "host": str(data),
                                                                      "mode": "ro"}]}}))
    sai = backend.SandboxAI()
    page = module.McpPage(sai)
    rows = [page.servers.item(row, 0).text() for row in range(page.servers.rowCount())]
    page.servers.selectRow(rows.index("own"))
    assert page.copy_to_button.text() == "Copy own to another host…" and page.copy_to_button.isEnabled()

    def resolve(dialog):
        assert dialog.target == "ct1"
        dialog.buttons.button(OK).click()
        assert "to resolve" in dialog.plan.toPlainText(), dialog.plan.toPlainText()
        dialog.folder_fields[str(data)][0].set_value(str(there_data))
        dialog.buttons.button(OK).click()
        return dialog.result()
    monkeypatch.setattr(module.McpCopyDialog, "exec", resolve)
    page.copy_to_host()
    defined = json.loads((fleet["there"] / ".config/sandbox-ai/mcp.json").read_text())["own"]
    assert defined["mounts"] == [{"target": "/data", "host": str(there_data), "mode": "ro"}]


@pytest.mark.parametrize("language", ["en", "de"])
def test_a_folder_capped_to_its_catalog_entrys_mode_is_said(language):
    """Round-1 review: a catalog server's folder is never more writable than its entry declares (mcp copy caps the
    source's mode): the plan's "capped" lines are shown with the rest."""
    from sandbox_ai_app import i18n, mcp_copy_view
    capped = "/work: read-only, as its catalog entry declares (the source has it writable; ...)"
    i18n.set_language(language)
    try:
        lines = mcp_copy_view.plan_lines({"to": "ct1", "servers": [{**PLAN_PATH, "problems": [], "capped": [capped]}]})
        assert ("  capped: " if language == "en" else "  begrenzt: ") + capped in lines, lines
    finally:
        i18n.set_language("en")
